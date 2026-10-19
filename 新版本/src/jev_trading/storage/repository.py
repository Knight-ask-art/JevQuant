"""Persistence repository.

Every write is parameterized through SQLAlchemy.  Financial amounts cross
the boundary as Decimal.  Audit events are append-only and hash-linked.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError

from ..domain.enums import (
    IntentType,
    Mode,
    OrderStatus,
    PositionStatus,
    SignalState,
    assert_signal_transition,
)
from ..domain.intent import ApprovedTradeIntent, TradeIntent
from ..domain.market import AccountSnapshot, MarketSnapshot
from ..domain.money import ZERO, quantize_nearest
from ..domain.policy import EffectivePolicy, canonical_json
from ..domain.risk import RiskDecision
from .db import Database
from .models import (
    AccountRow,
    AgentRunRow,
    ArmingTokenRow,
    AuditEventRow,
    BacktestRunRow,
    BalanceSnapshotRow,
    FillRow,
    JevEvaluationRow,
    MarketSnapshotRow,
    OrderEventRow,
    OrderRow,
    PnlSnapshotRow,
    PositionRow,
    RiskDecisionRow,
    SignalEventRow,
    SignalRow,
    StrategyPolicyRow,
    SystemStateRow,
    TradeIntentRow,
    utcnow,
)

GENESIS_HASH = "sha256:" + "0" * 64


def to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def usage_with_cache_split(usage: Any) -> dict[str, int]:
    """Normalize OpenAI/DeepSeek and Anthropic usage into one token shape.

    Cached prompt tokens are a subset of the prompt in both shapes, so they
    are reported separately rather than added.
    """

    data = usage if isinstance(usage, dict) else {}
    details = data.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    prompt = _as_int(data.get("prompt_tokens", data.get("input_tokens")))
    completion = _as_int(data.get("completion_tokens", data.get("output_tokens")))
    cached = _as_int(data.get("prompt_cache_hit_tokens", details.get("cached_tokens")))
    cached += _as_int(data.get("cache_read_input_tokens"))
    cached = min(cached, prompt) if prompt else cached
    total = _as_int(data.get("total_tokens")) or prompt + completion
    return {
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return 0


def utc_date(value: datetime) -> str:
    normalized = to_utc(value)
    assert normalized is not None
    return normalized.date().isoformat()


@dataclass(frozen=True)
class ExecutionFill:
    qty: Decimal
    price: Decimal
    fee_quote: Decimal
    fee_asset: str = "USDT"


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._audit_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # accounts
    # ------------------------------------------------------------------
    async def get_or_create_account(
        self, name: str, mode: Mode, starting_equity: Decimal
    ) -> AccountRow:
        async with self.db.session() as session:
            row = (
                await session.execute(select(AccountRow).where(AccountRow.name == name))
            ).scalar_one_or_none()
            if row is not None:
                return row
            row = AccountRow(
                name=name,
                mode=mode.value,
                starting_equity_usdt=starting_equity,
                cash_quote_usdt=starting_equity,
                peak_equity_usdt=starting_equity,
                daily_pnl_date=utc_date(utcnow()),
            )
            session.add(row)
            await session.flush()
            return row

    async def adjust_account_cash(
        self, account_id: str, *, cash_delta: Decimal, realized_delta: Decimal
    ) -> None:
        """Move settled cash for an exchange-managed account.

        Used when the venue confirms a fill the local projection had not
        applied yet (reconciliation), so the cash figure follows the venue.
        """

        async with self.db.session() as session:
            row = await session.get(AccountRow, account_id)
            if row is None:
                raise ValueError(f"account {account_id} not found")
            row.cash_quote_usdt += cash_delta
            row.realized_pnl_usdt += realized_delta
            row.daily_realized_pnl_usdt += realized_delta
            equity = row.cash_quote_usdt
            if equity > row.peak_equity_usdt:
                row.peak_equity_usdt = equity
            await session.flush()

    async def get_account(self, account_id: str) -> AccountRow | None:
        async with self.db.session() as session:
            return await session.get(AccountRow, account_id)

    async def list_accounts(self) -> list[AccountRow]:
        async with self.db.session() as session:
            return list((await session.execute(select(AccountRow))).scalars())

    async def get_open_positions(self, account_id: str) -> list[PositionRow]:
        async with self.db.session() as session:
            result = await session.execute(
                select(PositionRow)
                .where(
                    PositionRow.account_id == account_id,
                    PositionRow.status == PositionStatus.OPEN.value,
                )
                .order_by(PositionRow.opened_at)
            )
            return list(result.scalars())

    async def request_close(
        self, position_id: str, *, reason: str, actor: str
    ) -> PositionRow | None:
        """Record an operator close request on an open position.

        Returns ``None`` when the position is missing or already closed, so
        the caller can answer with NOT_FOUND rather than queueing an exit for
        something that does not exist. The exit manager consumes the request.
        """

        async with self.db.session() as session:
            row = await session.get(PositionRow, position_id)
            if row is None or row.status != PositionStatus.OPEN.value:
                return None
            row.exit_requested_at = utcnow()
            row.exit_requested_reason = reason
            await session.flush()
            return row

    async def settled_position(self, position_id: str) -> PositionRow:
        """A position row freshly read from the store (never a stale identity)."""

        async with self.db.session() as session:
            row = await session.get(PositionRow, position_id)
            if row is None:
                raise ValueError(f"position {position_id} not found")
            return row

    async def get_open_position(self, account_id: str, symbol: str) -> PositionRow | None:
        async with self.db.session() as session:
            return (
                await session.execute(
                    select(PositionRow).where(
                        PositionRow.account_id == account_id,
                        PositionRow.symbol == symbol,
                        PositionRow.status == PositionStatus.OPEN.value,
                    )
                )
            ).scalar_one_or_none()

    async def last_entry_at(self, account_id: str, symbol: str) -> datetime | None:
        async with self.db.session() as session:
            value = (
                await session.execute(
                    select(func.max(PositionRow.opened_at)).where(
                        PositionRow.account_id == account_id,
                        PositionRow.symbol == symbol,
                    )
                )
            ).scalar_one_or_none()
            return to_utc(value)

    async def account_snapshot(
        self,
        account_id: str,
        marks: dict[str, Decimal],
        now: datetime | None = None,
    ) -> AccountSnapshot:
        now = now or utcnow()
        async with self.db.session() as session:
            account = await session.get(AccountRow, account_id)
            if account is None:
                raise ValueError(f"account {account_id} not found")
            positions = list(
                (
                    await session.execute(
                        select(PositionRow).where(
                            PositionRow.account_id == account_id,
                            PositionRow.status == PositionStatus.OPEN.value,
                        )
                    )
                ).scalars()
            )
            today = utc_date(now)
            if account.daily_pnl_date != today:
                account.daily_pnl_date = today
                account.daily_realized_pnl_usdt = ZERO

            gross = ZERO
            unrealized = ZERO
            for position in positions:
                mark = marks.get(position.symbol, position.avg_entry_price)
                gross += position.qty * mark
                unrealized += position.qty * (mark - position.avg_entry_price)
            equity = account.cash_quote_usdt + gross
            if equity > account.peak_equity_usdt:
                account.peak_equity_usdt = equity
            peak = account.peak_equity_usdt
            drawdown_pct = (
                (peak - equity) / peak * Decimal("100") if peak > ZERO else ZERO
            )
            daily_pct = (
                account.daily_realized_pnl_usdt / account.starting_equity_usdt
                * Decimal("100")
                if account.starting_equity_usdt > ZERO
                else ZERO
            )
            await session.flush()
            quantum = Decimal("0.00000001")
            return AccountSnapshot(
                equity_usdt=quantize_nearest(equity, quantum),
                available_quote_usdt=quantize_nearest(
                    account.cash_quote_usdt, quantum
                ),
                gross_exposure_usdt=quantize_nearest(gross, quantum),
                open_positions=len(positions),
                daily_realized_pnl_usdt=quantize_nearest(
                    account.daily_realized_pnl_usdt, quantum
                ),
                daily_realized_pnl_pct=quantize_nearest(
                    daily_pct, Decimal("0.000001")
                ),
                drawdown_pct=quantize_nearest(drawdown_pct, Decimal("0.000001")),
            )

    # ------------------------------------------------------------------
    # strategy policies
    # ------------------------------------------------------------------
    async def insert_policy(
        self,
        effective: EffectivePolicy,
        *,
        source: str,
        is_simulated: bool,
    ) -> StrategyPolicyRow:
        async with self.db.session() as session:
            previous = list(
                (
                    await session.execute(
                        select(StrategyPolicyRow).where(StrategyPolicyRow.status == "ACTIVE")
                    )
                ).scalars()
            )
            for row in previous:
                row.status = "SUPERSEDED"
            row = StrategyPolicyRow(
                policy_id=str(effective.policy_id),
                status=effective.status.value,
                source=source,
                is_simulated=is_simulated,
                raw_json=effective.raw_policy.model_dump(mode="json"),
                effective_json=effective.model_dump(mode="json"),
                mode_scope_json=[mode.value for mode in effective.mode_scope],
                clamps_json=[clamp.model_dump(mode="json") for clamp in effective.clamps],
                policy_hash=effective.policy_hash,
                generated_at=effective.generated_at,
                expires_at=effective.expires_at,
            )
            session.add(row)
            await session.flush()
            return row

    async def get_active_policy(self) -> StrategyPolicyRow | None:
        async with self.db.session() as session:
            return (
                await session.execute(
                    select(StrategyPolicyRow)
                    .where(StrategyPolicyRow.status == "ACTIVE")
                    .order_by(StrategyPolicyRow.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def get_latest_policy(self) -> StrategyPolicyRow | None:
        """The most recently written policy row, whatever its status.

        Used only to explain a blocked state: when nothing is ACTIVE, the
        newest row tells the operator whether the desk expired or was never
        authored.
        """

        async with self.db.session() as session:
            return (
                await session.execute(
                    select(StrategyPolicyRow)
                    .order_by(StrategyPolicyRow.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def get_policy_by_policy_id(self, policy_id: str) -> StrategyPolicyRow | None:
        async with self.db.session() as session:
            return (
                await session.execute(
                    select(StrategyPolicyRow)
                    .where(StrategyPolicyRow.policy_id == policy_id)
                    .order_by(StrategyPolicyRow.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def list_policies(self, limit: int = 50) -> list[StrategyPolicyRow]:
        async with self.db.session() as session:
            return list(
                (
                    await session.execute(
                        select(StrategyPolicyRow)
                        .order_by(StrategyPolicyRow.created_at.desc())
                        .limit(limit)
                    )
                ).scalars()
            )

    async def expire_policies(self, now: datetime | None = None) -> int:
        now = now or utcnow()
        async with self.db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(StrategyPolicyRow).where(
                            StrategyPolicyRow.status == "ACTIVE",
                            StrategyPolicyRow.expires_at <= now,
                        )
                    )
                ).scalars()
            )
            for row in rows:
                row.status = "EXPIRED"
            return len(rows)

    # ------------------------------------------------------------------
    # strategy agent journal
    # ------------------------------------------------------------------
    async def insert_agent_run(
        self,
        *,
        mode: str,
        provider: str,
        model: str,
        status: str,
        turns: int = 0,
        tool_calls: list[dict[str, Any]] | None = None,
        usage: dict[str, Any] | None = None,
        latency_ms: int = 0,
        rationale: str | None = None,
        cycle_id: str | None = None,
        policy_id: str | None = None,
        error_code: str | None = None,
    ) -> AgentRunRow:
        async with self.db.session() as session:
            row = AgentRunRow(
                cycle_id=cycle_id,
                policy_id=policy_id,
                mode=mode,
                provider=provider,
                model=model,
                status=status,
                turns=turns,
                tool_calls_json=tool_calls or [],
                usage_json=usage or {},
                latency_ms=latency_ms,
                rationale=rationale,
                error_code=error_code,
            )
            session.add(row)
            await session.flush()
            return row

    async def list_agent_runs(self, limit: int = 30) -> list[AgentRunRow]:
        async with self.db.session() as session:
            return list(
                (
                    await session.execute(
                        select(AgentRunRow)
                        .order_by(AgentRunRow.created_at.desc())
                        .limit(limit)
                    )
                ).scalars()
            )

    async def get_agent_run(self, run_id: str) -> AgentRunRow | None:
        async with self.db.session() as session:
            return await session.get(AgentRunRow, run_id)

    async def latest_agent_run_for_policy(
        self, policy_id: str
    ) -> AgentRunRow | None:
        async with self.db.session() as session:
            return (
                await session.execute(
                    select(AgentRunRow)
                    .where(AgentRunRow.policy_id == policy_id)
                    .order_by(AgentRunRow.created_at.desc())
                    .limit(1)
                )
            ).scalars().first()

    async def signal_counts_for_policies(
        self, policy_ids: list[str]
    ) -> dict[str, dict[str, int]]:
        """Signal state counts per policy, for the agent's decision journal."""

        if not policy_ids:
            return {}
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    select(SignalRow.policy_id, SignalRow.state, func.count())
                    .where(SignalRow.policy_id.in_(policy_ids))
                    .group_by(SignalRow.policy_id, SignalRow.state)
                )
            ).all()
        counts: dict[str, dict[str, int]] = {}
        for policy_id, state, count in rows:
            counts.setdefault(policy_id, {})[state] = int(count)
        return counts

    async def closed_positions_for_policies(
        self, policy_ids: list[str]
    ) -> list[PositionRow]:
        if not policy_ids:
            return []
        async with self.db.session() as session:
            return list(
                (
                    await session.execute(
                        select(PositionRow).where(
                            PositionRow.policy_id.in_(policy_ids),
                            PositionRow.status == PositionStatus.CLOSED.value,
                        )
                    )
                ).scalars()
            )

    # ------------------------------------------------------------------
    # market snapshots
    # ------------------------------------------------------------------
    async def insert_market_snapshot(self, snapshot: MarketSnapshot) -> MarketSnapshotRow:
        async with self.db.session() as session:
            row = MarketSnapshotRow(
                id=str(snapshot.snapshot_id),
                symbol=snapshot.symbol,
                as_of=snapshot.as_of,
                source=snapshot.source,
                bid=snapshot.bid,
                ask=snapshot.ask,
                spread_bps=snapshot.spread_bps,
                candle_close_at=snapshot.candle_close_at,
                features_json=snapshot.features,
                is_synthetic=snapshot.is_synthetic,
            )
            session.add(row)
            await session.flush()
            return row

    async def list_market_snapshots(self, limit: int = 100) -> list[MarketSnapshotRow]:
        async with self.db.session() as session:
            return list(
                (
                    await session.execute(
                        select(MarketSnapshotRow)
                        .order_by(MarketSnapshotRow.created_at.desc())
                        .limit(limit)
                    )
                ).scalars()
            )

    # ------------------------------------------------------------------
    # signals and Jev evaluations
    # ------------------------------------------------------------------
    async def create_signal(
        self,
        *,
        symbol: str,
        policy_id: str,
        snapshot_id: str,
        side: str,
        state: SignalState = SignalState.CANDIDATE,
        detail: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> SignalRow:
        async with self.db.session() as session:
            row = SignalRow(
                symbol=symbol,
                state=state.value,
                policy_id=policy_id,
                snapshot_id=snapshot_id,
                side=side,
            )
            if created_at is not None:
                # Backdated signal (demo clocks, replay); the caller owns the
                # timestamp rather than the column default.
                row.created_at = created_at
            session.add(row)
            await session.flush()
            session.add(
                SignalEventRow(
                    signal_id=row.id,
                    state=state.value,
                    detail_json=detail or {},
                )
            )
            await session.flush()
            return row

    async def recent_entry_signal_times(
        self, symbol: str, since: datetime, states: Sequence[str]
    ) -> list[datetime]:
        """When entries for ``symbol`` reached the market since ``since``.

        Over-trading protection counts entries that got past the Jev gate, not
        every signal the cycle evaluated: throttling evaluation would starve
        the agent of the evidence it calibrates against. Signals the risk
        engine later turned down are included - they still cost a round trip
        of work, and a symbol that keeps producing them is churning.
        """

        async with self.db.session() as session:
            result = await session.execute(
                select(SignalRow.created_at)
                .where(
                    SignalRow.symbol == symbol,
                    SignalRow.created_at >= since,
                    SignalRow.state.in_(list(states)),
                )
                .order_by(SignalRow.created_at)
            )
            return list(result.scalars())

    async def get_signal(self, signal_id: str) -> SignalRow | None:
        async with self.db.session() as session:
            return await session.get(SignalRow, signal_id)

    async def list_signals(
        self, limit: int = 100, state: SignalState | None = None
    ) -> list[SignalRow]:
        async with self.db.session() as session:
            query = select(SignalRow).order_by(SignalRow.created_at.desc()).limit(limit)
            if state is not None:
                query = query.where(SignalRow.state == state.value)
            return list((await session.execute(query)).scalars())

    async def list_signals_for_policy(self, policy_id: str, limit: int = 200) -> list[SignalRow]:
        async with self.db.session() as session:
            return list(
                (
                    await session.execute(
                        select(SignalRow)
                        .where(SignalRow.policy_id == policy_id)
                        .order_by(SignalRow.created_at.desc())
                        .limit(limit)
                    )
                ).scalars()
            )

    async def get_signal_events(self, signal_id: str) -> list[SignalEventRow]:
        async with self.db.session() as session:
            return list(
                (
                    await session.execute(
                        select(SignalEventRow)
                        .where(SignalEventRow.signal_id == signal_id)
                        .order_by(SignalEventRow.created_at)
                    )
                ).scalars()
            )

    async def transition_signal(
        self,
        signal_id: str,
        new_state: SignalState,
        detail: dict[str, Any] | None = None,
    ) -> SignalRow:
        async with self.db.session() as session:
            row = await session.get(SignalRow, signal_id)
            if row is None:
                raise ValueError(f"signal {signal_id} not found")
            assert_signal_transition(SignalState(row.state), new_state)
            row.state = new_state.value
            row.updated_at = utcnow()
            if new_state is SignalState.ERROR and detail and detail.get("reason"):
                row.reason = str(detail["reason"])[:2000]
            session.add(
                SignalEventRow(
                    signal_id=signal_id,
                    state=new_state.value,
                    detail_json=detail or {},
                )
            )
            await session.flush()
            return row

    async def attach_jev_evaluation(
        self,
        signal_id: str,
        evaluation_id: str,
        score: dict[str, Any] | None = None,
    ) -> None:
        async with self.db.session() as session:
            row = await session.get(SignalRow, signal_id)
            if row is None:
                raise ValueError(f"signal {signal_id} not found")
            row.jev_evaluation_id = evaluation_id
            if score is not None:
                row.score_json = score
            row.updated_at = utcnow()

    async def insert_jev_evaluation(
        self,
        *,
        signal_id: str | None,
        model: str,
        request: dict[str, Any],
        response: dict[str, Any],
        answers: dict[str, Any],
        latency_ms: int = 0,
        error_code: str | None = None,
    ) -> JevEvaluationRow:
        async with self.db.session() as session:
            row = JevEvaluationRow(
                signal_id=signal_id,
                model=model,
                request_json=request,
                response_json=response,
                answers_json=answers,
                latency_ms=latency_ms,
                error_code=error_code,
            )
            session.add(row)
            await session.flush()
            return row

    async def get_jev_evaluation(self, evaluation_id: str) -> JevEvaluationRow | None:
        async with self.db.session() as session:
            return await session.get(JevEvaluationRow, evaluation_id)

    # ------------------------------------------------------------------
    # intents and risk decisions
    # ------------------------------------------------------------------
    async def insert_intent(
        self, intent: TradeIntent, *, status: str = "RISK_PENDING"
    ) -> tuple[TradeIntentRow, bool]:
        async with self.db.session() as session:
            existing = (
                await session.execute(
                    select(TradeIntentRow).where(
                        TradeIntentRow.idempotency_key == intent.idempotency_key
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing, False
            row = TradeIntentRow(
                id=str(intent.intent_id),
                signal_id=str(intent.signal_id),
                policy_id=str(intent.policy_id),
                account_id=str(intent.account_id),
                mode=intent.mode.value,
                symbol=intent.symbol,
                side=intent.side.value,
                intent_type=intent.intent_type.value,
                order_preference=intent.order_preference.value,
                quote_notional=intent.quote_notional,
                max_slippage_bps=intent.max_slippage_bps,
                limit_price=intent.limit_price,
                stop_loss_bps=intent.stop_loss_bps,
                take_profit_bps=intent.take_profit_bps,
                max_holding_seconds=intent.max_holding_seconds,
                status=status,
                created_at=intent.created_at,
                expires_at=intent.expires_at,
                idempotency_key=intent.idempotency_key,
                risk_snapshot_hash=intent.risk_snapshot_hash,
                provenance_json=intent.provenance.model_dump(mode="json"),
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                existing = (
                    await session.execute(
                        select(TradeIntentRow).where(
                            TradeIntentRow.idempotency_key == intent.idempotency_key
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                return existing, False
            return row, True

    async def get_signal_intent(
        self, signal_id: str, *, intent_type: IntentType
    ) -> TradeIntentRow | None:
        """The intent a signal is currently acting on (latest of its kind)."""

        async with self.db.session() as session:
            return (
                await session.execute(
                    select(TradeIntentRow)
                    .where(
                        TradeIntentRow.signal_id == signal_id,
                        TradeIntentRow.intent_type == intent_type.value,
                    )
                    .order_by(TradeIntentRow.created_at.desc())
                    .limit(1)
                )
            ).scalars().first()

    async def get_intent(self, intent_id: str) -> TradeIntentRow | None:
        async with self.db.session() as session:
            return await session.get(TradeIntentRow, intent_id)

    async def latest_unsettled_close_intent(
        self, account_id: str, symbol: str, mode: str
    ) -> TradeIntentRow | None:
        """The most recent close intent on a route that may still be working."""

        async with self.db.session() as session:
            return (
                await session.execute(
                    select(TradeIntentRow)
                    .where(
                        TradeIntentRow.account_id == account_id,
                        TradeIntentRow.symbol == symbol,
                        TradeIntentRow.mode == mode,
                        TradeIntentRow.intent_type == IntentType.CLOSE.value,
                        TradeIntentRow.status.in_(
                            [
                                "APPROVED",
                                "EXECUTION_PENDING",
                                "EXECUTED",
                                OrderStatus.ACCEPTED.value,
                                OrderStatus.PARTIALLY_FILLED.value,
                                OrderStatus.UNKNOWN.value,
                            ]
                        ),
                    )
                    .order_by(TradeIntentRow.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def get_intent_by_idempotency(self, key: str) -> TradeIntentRow | None:
        async with self.db.session() as session:
            return (
                await session.execute(
                    select(TradeIntentRow).where(TradeIntentRow.idempotency_key == key)
                )
            ).scalar_one_or_none()

    async def list_intents(self, limit: int = 100) -> list[TradeIntentRow]:
        async with self.db.session() as session:
            return list(
                (
                    await session.execute(
                        select(TradeIntentRow)
                        .order_by(TradeIntentRow.created_at.desc())
                        .limit(limit)
                    )
                ).scalars()
            )

    async def update_intent_status(self, intent_id: str, status: str) -> TradeIntentRow:
        async with self.db.session() as session:
            row = await session.get(TradeIntentRow, intent_id)
            if row is None:
                raise ValueError(f"intent {intent_id} not found")
            row.status = status
            await session.flush()
            return row

    async def insert_risk_decision(self, decision: RiskDecision) -> RiskDecisionRow:
        async with self.db.session() as session:
            row = RiskDecisionRow(
                id=str(decision.decision_id),
                intent_id=str(decision.intent_id),
                approved=decision.approved,
                failed_rules_json=[
                    failure.model_dump(mode="json") for failure in decision.failed_rules
                ],
                effective_json=decision.effective,
                snapshot_hash=decision.snapshot_hash,
                decision_hash=decision.decision_hash,
                approved_intent_json=(
                    decision.approved_intent.model_dump(mode="json")
                    if decision.approved_intent is not None
                    else None
                ),
            )
            session.add(row)
            intent = await session.get(TradeIntentRow, str(decision.intent_id))
            if intent is not None:
                intent.status = "APPROVED" if decision.approved else "RISK_REJECTED"
            await session.flush()
            return row

    async def get_risk_decision(self, decision_id: str) -> RiskDecisionRow | None:
        async with self.db.session() as session:
            return await session.get(RiskDecisionRow, decision_id)

    async def get_risk_decision_for_intent(self, intent_id: str) -> RiskDecisionRow | None:
        async with self.db.session() as session:
            return (
                await session.execute(
                    select(RiskDecisionRow)
                    .where(RiskDecisionRow.intent_id == intent_id)
                    .order_by(RiskDecisionRow.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def list_risk_decisions(self, limit: int = 100) -> list[RiskDecisionRow]:
        async with self.db.session() as session:
            return list(
                (
                    await session.execute(
                        select(RiskDecisionRow)
                        .order_by(RiskDecisionRow.created_at.desc())
                        .limit(limit)
                    )
                ).scalars()
            )

    # ------------------------------------------------------------------
    # execution ledger
    # ------------------------------------------------------------------
    async def record_execution(
        self,
        *,
        approved: ApprovedTradeIntent,
        order_id: str,
        exchange_order_id: str | None,
        status: OrderStatus,
        fills: list[ExecutionFill],
        account_id: str,
        position: PositionRow | None = None,
        now: datetime | None = None,
    ) -> tuple[OrderRow, PositionRow | None]:
        now = now or utcnow()
        intent = approved.intent
        filled_qty = sum((f.qty for f in fills), ZERO)
        fee_quote = sum((f.fee_quote for f in fills), ZERO)
        if filled_qty > ZERO:
            weighted = sum((f.qty * f.price for f in fills), ZERO)
            avg_price = weighted / filled_qty
        else:
            avg_price = ZERO
        is_close = intent.intent_type is IntentType.CLOSE

        async with self.db.session() as session:
            order = OrderRow(
                id=order_id,
                intent_id=str(intent.intent_id),
                exchange_order_id=exchange_order_id,
                symbol=intent.symbol,
                side=intent.side.value,
                order_type=intent.order_preference.value,
                qty=approved.approved_qty,
                price=approved.approved_limit_price,
                status=status.value,
                filled_qty=filled_qty,
                avg_fill_price=avg_price,
                fee_quote=fee_quote,
                mode=intent.mode.value,
                is_simulated=intent.mode is not Mode.LIVE,
            )
            session.add(order)
            # Fills carry a foreign key to the order. Without a relationship
            # between the mappers SQLAlchemy does not order the two inserts,
            # and PostgreSQL rejects the fill before the order exists. Flush
            # the order explicitly so the ledger works on both backends.
            await session.flush()
            for fill in fills:
                session.add(
                    FillRow(
                        order_id=order_id,
                        qty=fill.qty,
                        price=fill.price,
                        fee_quote=fill.fee_quote,
                        fee_asset=fill.fee_asset,
                    )
                )
            session.add(
                OrderEventRow(
                    order_id=order_id,
                    event_type=status.value,
                    payload_json={
                        "filled_qty": str(filled_qty),
                        "avg_fill_price": str(avg_price),
                        "fee_quote": str(fee_quote),
                    },
                )
            )

            account = await session.get(AccountRow, account_id)
            if account is None:
                raise ValueError(f"account {account_id} not found")

            result_position: PositionRow | None = None
            if not is_close:
                if filled_qty > ZERO:
                    result_position = PositionRow(
                        account_id=account_id,
                        symbol=intent.symbol,
                        qty=filled_qty,
                        avg_entry_price=avg_price,
                        stop_loss_bps=approved.effective_stop_loss_bps
                        or Decimal("0"),
                        take_profit_bps=approved.effective_take_profit_bps
                        or Decimal("0"),
                        max_holding_seconds=approved.effective_max_holding_seconds or 0,
                        status=PositionStatus.OPEN.value,
                        opened_at=now,
                        entry_order_id=order_id,
                        policy_id=str(intent.policy_id),
                        signal_id=str(intent.signal_id),
                        mode=intent.mode.value,
                        is_simulated=intent.mode is not Mode.LIVE,
                    )
                    session.add(result_position)
                    await session.flush()
                    account.cash_quote_usdt -= filled_qty * avg_price + fee_quote
            else:
                if position is not None:
                    position = await session.get(PositionRow, position.id)
                if position is None:
                    position = (
                        await session.execute(
                            select(PositionRow).where(
                                PositionRow.account_id == account_id,
                                PositionRow.symbol == intent.symbol,
                                PositionRow.status == PositionStatus.OPEN.value,
                            )
                        )
                    ).scalar_one_or_none()
                if position is None:
                    raise ValueError("cannot record a close without an open position")
                # The entry fee left the account when the position opened, so it
                # is a realized cost of this trade. Allocate it per unit from the
                # entry order's fill so partial closes stay exact.
                entry_fee_alloc = ZERO
                if position.entry_order_id:
                    entry_order = await session.get(OrderRow, position.entry_order_id)
                    if (
                        entry_order is not None
                        and entry_order.filled_qty > ZERO
                        and entry_order.fee_quote > ZERO
                    ):
                        entry_fee_alloc = (
                            entry_order.fee_quote * filled_qty / entry_order.filled_qty
                        )
                realized = (
                    filled_qty * (avg_price - position.avg_entry_price)
                    - fee_quote
                    - entry_fee_alloc
                )
                account.cash_quote_usdt += filled_qty * avg_price - fee_quote
                account.realized_pnl_usdt += realized
                account.daily_realized_pnl_usdt += realized
                position.realized_pnl_usdt += realized
                position.exit_order_id = order_id
                remaining = position.qty - filled_qty
                if remaining <= position.qty * Decimal("0.00000001"):
                    position.qty = ZERO
                    position.status = PositionStatus.CLOSED.value
                    position.closed_at = now
                else:
                    position.qty = remaining
                result_position = position

            await session.flush()
            return order, result_position

    async def record_signal_order_link(self, signal_id: str, order_id: str) -> None:
        async with self.db.session() as session:
            session.add(
                OrderEventRow(
                    order_id=order_id,
                    event_type="SIGNAL_LINKED",
                    payload_json={"signal_id": signal_id},
                )
            )

    async def record_order_event(
        self, order_id: str, event_type: str, payload: dict[str, Any] | None = None
    ) -> None:
        async with self.db.session() as session:
            session.add(
                OrderEventRow(
                    order_id=order_id,
                    event_type=event_type,
                    payload_json=payload or {},
                )
            )

    async def get_order(self, order_id: str) -> OrderRow | None:
        async with self.db.session() as session:
            return await session.get(OrderRow, order_id)

    async def list_orders(self, limit: int = 100) -> list[OrderRow]:
        async with self.db.session() as session:
            return list(
                (
                    await session.execute(
                        select(OrderRow).order_by(OrderRow.created_at.desc()).limit(limit)
                    )
                ).scalars()
            )

    async def list_positions(
        self, limit: int = 100, open_only: bool = False
    ) -> list[PositionRow]:
        async with self.db.session() as session:
            query = select(PositionRow).order_by(PositionRow.opened_at.desc()).limit(limit)
            if open_only:
                query = query.where(PositionRow.status == PositionStatus.OPEN.value)
            return list((await session.execute(query)).scalars())

    async def list_fills(self, limit: int = 100) -> list[FillRow]:
        async with self.db.session() as session:
            return list(
                (
                    await session.execute(
                        select(FillRow).order_by(FillRow.created_at.desc()).limit(limit)
                    )
                ).scalars()
            )

    async def list_fills_for_order(self, order_id: str) -> list[FillRow]:
        async with self.db.session() as session:
            return list(
                (
                    await session.execute(
                        select(FillRow)
                        .where(FillRow.order_id == order_id)
                        .order_by(FillRow.created_at)
                    )
                ).scalars()
            )

    async def add_exchange_fill(
        self,
        *,
        order_id: str,
        trade_id: str,
        qty: Decimal,
        price: Decimal,
        fee_quote: Decimal,
        fee_asset: str = "USDT",
        created_at: datetime | None = None,
    ) -> FillRow | None:
        """Append one exchange-reported fill exactly once.

        Idempotent per (order, trade id): a reconciliation pass that sees the
        same trade twice must not double-count it.
        """

        if qty <= ZERO:
            return None
        async with self.db.session() as session:
            existing = await self._exchange_fill(session, order_id, trade_id)
            if existing is not None:
                return existing
            row = FillRow(
                order_id=order_id,
                external_id=str(trade_id),
                qty=qty,
                price=price,
                fee_quote=fee_quote,
                fee_asset=fee_asset,
                created_at=created_at or utcnow(),
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                # A concurrent pass inserted the same trade first; it wins.
                await session.rollback()
                return await self._exchange_fill(session, order_id, trade_id)
            return row

    @staticmethod
    async def _exchange_fill(session, order_id: str, trade_id: str) -> FillRow | None:
        return (
            await session.execute(
                select(FillRow).where(
                    FillRow.order_id == order_id,
                    FillRow.external_id == str(trade_id),
                )
            )
        ).scalars().first()

    async def set_position_realized_pnl(
        self, position_id: str, realized_pnl_usdt: Decimal
    ) -> None:
        async with self.db.session() as session:
            row = await session.get(PositionRow, position_id)
            if row is None:
                raise ValueError(f"position {position_id} not found")
            row.realized_pnl_usdt = realized_pnl_usdt
            await session.flush()

    # ------------------------------------------------------------------
    # exchange balance snapshots
    # ------------------------------------------------------------------
    async def insert_balance_snapshot(self, row: BalanceSnapshotRow) -> BalanceSnapshotRow:
        async with self.db.session() as session:
            session.add(row)
            await session.flush()
            return row

    async def latest_balance_snapshots(self) -> list[BalanceSnapshotRow]:
        """The most recent snapshot for each route/asset pair."""

        async with self.db.session() as session:
            latest = (
                select(
                    BalanceSnapshotRow.source,
                    BalanceSnapshotRow.asset,
                    func.max(BalanceSnapshotRow.created_at).label("as_of"),
                )
                .group_by(BalanceSnapshotRow.source, BalanceSnapshotRow.asset)
                .subquery()
            )
            rows = (
                await session.execute(
                    select(BalanceSnapshotRow).join(
                        latest,
                        and_(
                            BalanceSnapshotRow.source == latest.c.source,
                            BalanceSnapshotRow.asset == latest.c.asset,
                            BalanceSnapshotRow.created_at == latest.c.as_of,
                        ),
                    )
                )
            ).scalars()
            return list(rows)

    # ------------------------------------------------------------------
    # PnL snapshots
    # ------------------------------------------------------------------
    async def insert_pnl_snapshot(
        self,
        *,
        account_id: str,
        as_of: datetime,
        equity_usdt: Decimal,
        cash_quote_usdt: Decimal,
        realized_pnl_usdt: Decimal,
        unrealized_pnl_usdt: Decimal,
        gross_exposure_usdt: Decimal,
        daily_realized_pnl_usdt: Decimal,
        drawdown_pct: Decimal,
    ) -> PnlSnapshotRow:
        async with self.db.session() as session:
            row = PnlSnapshotRow(
                account_id=account_id,
                as_of=as_of,
                equity_usdt=equity_usdt,
                cash_quote_usdt=cash_quote_usdt,
                realized_pnl_usdt=realized_pnl_usdt,
                unrealized_pnl_usdt=unrealized_pnl_usdt,
                gross_exposure_usdt=gross_exposure_usdt,
                daily_realized_pnl_usdt=daily_realized_pnl_usdt,
                drawdown_pct=drawdown_pct,
            )
            session.add(row)
            await session.flush()
            return row

    async def list_pnl_snapshots(self, limit: int = 200) -> list[PnlSnapshotRow]:
        async with self.db.session() as session:
            return list(
                (
                    await session.execute(
                        select(PnlSnapshotRow)
                        .order_by(PnlSnapshotRow.as_of.desc())
                        .limit(limit)
                    )
                ).scalars()
            )

    # ------------------------------------------------------------------
    # append-only, hash-linked audit stream
    # ------------------------------------------------------------------
    async def append_audit(
        self,
        event_type: str,
        entity_type: str,
        entity_id: str | None,
        payload: dict[str, Any] | None = None,
    ) -> AuditEventRow:
        import hashlib

        async with self._audit_lock:
            async with self.db.session() as session:
                last = (
                    await session.execute(
                        select(AuditEventRow).order_by(AuditEventRow.seq.desc()).limit(1)
                    )
                ).scalar_one_or_none()
                prev_hash = last.event_hash if last is not None else GENESIS_HASH
                created_at = utcnow()
                safe_payload = payload or {}
                body = {
                    "event_type": event_type,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "payload": safe_payload,
                    "created_at": created_at.isoformat(),
                }
                digest = hashlib.sha256(
                    (prev_hash + canonical_json(body)).encode("utf-8")
                ).hexdigest()
                row = AuditEventRow(
                    event_type=event_type,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    payload_json=safe_payload,
                    prev_hash=prev_hash,
                    event_hash="sha256:" + digest,
                    created_at=created_at,
                )
                session.add(row)
                await session.flush()
                return row

    async def list_audit(
        self, limit: int = 200, event_type: str | None = None
    ) -> list[AuditEventRow]:
        async with self.db.session() as session:
            query = select(AuditEventRow).order_by(AuditEventRow.seq.desc()).limit(limit)
            if event_type:
                query = query.where(AuditEventRow.event_type == event_type)
            return list((await session.execute(query)).scalars())

    async def verify_audit_chain(self) -> dict[str, Any]:
        import hashlib

        async with self.db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(AuditEventRow).order_by(AuditEventRow.seq)
                    )
                ).scalars()
            )
        prev = GENESIS_HASH
        for row in rows:
            body = {
                "event_type": row.event_type,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "payload": row.payload_json,
                "created_at": to_utc(row.created_at).isoformat() if row.created_at else "",
            }
            expected = "sha256:" + hashlib.sha256(
                (prev + canonical_json(body)).encode("utf-8")
            ).hexdigest()
            if row.prev_hash != prev or row.event_hash != expected:
                return {
                    "valid": False,
                    "count": len(rows),
                    "broken_seq": row.seq,
                }
            prev = row.event_hash
        return {"valid": True, "count": len(rows), "head": prev}

    # ------------------------------------------------------------------
    # system state / arming / backtests
    # ------------------------------------------------------------------
    async def get_state(self, key: str, default: Any = None) -> Any:
        async with self.db.session() as session:
            row = await session.get(SystemStateRow, key)
            if row is None:
                return default
            return row.value_json.get("value", default)

    async def set_state(self, key: str, value: Any) -> None:
        async with self.db.session() as session:
            row = await session.get(SystemStateRow, key)
            if row is None:
                session.add(SystemStateRow(key=key, value_json={"value": value}))
            else:
                row.value_json = {"value": value}
                row.updated_at = utcnow()

    async def get_all_state(self) -> dict[str, Any]:
        async with self.db.session() as session:
            rows = list((await session.execute(select(SystemStateRow))).scalars())
            return {row.key: row.value_json.get("value") for row in rows}

    async def create_arming_token(
        self, token_hash: str, expires_at: datetime, preflight: dict[str, Any]
    ) -> ArmingTokenRow:
        async with self.db.session() as session:
            row = ArmingTokenRow(
                token_hash=token_hash,
                expires_at=expires_at,
                preflight_json=preflight,
            )
            session.add(row)
            await session.flush()
            return row

    async def get_arming_token(self, token_hash: str) -> ArmingTokenRow | None:
        async with self.db.session() as session:
            return await session.get(ArmingTokenRow, token_hash)

    async def consume_arming_token(
        self, token_hash: str, now: datetime | None = None
    ) -> ArmingTokenRow | None:
        now = now or utcnow()
        async with self.db.session() as session:
            row = await session.get(ArmingTokenRow, token_hash)
            if row is None or row.used:
                return None
            if to_utc(row.expires_at) <= now:
                return None
            row.used = True
            await session.flush()
            return row

    async def create_backtest_run(self, request: dict[str, Any]) -> BacktestRunRow:
        async with self.db.session() as session:
            row = BacktestRunRow(status="QUEUED", request_json=request)
            session.add(row)
            await session.flush()
            return row

    async def update_backtest_run(
        self,
        run_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> BacktestRunRow:
        async with self.db.session() as session:
            row = await session.get(BacktestRunRow, run_id)
            if row is None:
                raise ValueError(f"backtest {run_id} not found")
            row.status = status
            if result is not None:
                row.result_json = result
            row.error_code = error_code
            if status in {"COMPLETED", "FAILED"}:
                row.completed_at = utcnow()
            await session.flush()
            return row

    async def get_backtest_run(self, run_id: str) -> BacktestRunRow | None:
        async with self.db.session() as session:
            return await session.get(BacktestRunRow, run_id)

    async def list_backtest_runs(self, limit: int = 50) -> list[BacktestRunRow]:
        async with self.db.session() as session:
            return list(
                (
                    await session.execute(
                        select(BacktestRunRow)
                        .order_by(BacktestRunRow.created_at.desc())
                        .limit(limit)
                    )
                ).scalars()
            )

    async def update_order_from_exchange(
        self,
        order_id: str,
        *,
        status: str,
        filled_qty: Decimal | None = None,
        avg_fill_price: Decimal | None = None,
        fee_quote: Decimal | None = None,
        note: str = "",
    ) -> OrderRow:
        async with self.db.session() as session:
            row = await session.get(OrderRow, order_id)
            if row is None:
                raise ValueError(f"order {order_id} not found")
            row.status = status
            if filled_qty is not None:
                row.filled_qty = filled_qty
            if avg_fill_price is not None:
                row.avg_fill_price = avg_fill_price
            if fee_quote is not None:
                row.fee_quote = fee_quote
            session.add(
                OrderEventRow(
                    order_id=order_id,
                    event_type="RECONCILED",
                    payload_json={"status": status, "note": note},
                )
            )
            await session.flush()
            return row

    async def record_order_row(
        self,
        *,
        approved: ApprovedTradeIntent,
        order_id: str,
        status: OrderStatus,
        now: datetime | None = None,
    ) -> None:
        """Persist an order without applying any fill to the ledger."""

        intent = approved.intent
        async with self.db.session() as session:
            session.add(
                OrderRow(
                    id=order_id,
                    intent_id=str(intent.intent_id),
                    exchange_order_id=None,
                    symbol=intent.symbol,
                    side=intent.side.value,
                    order_type=intent.order_preference.value,
                    qty=approved.approved_qty,
                    price=approved.approved_limit_price,
                    status=status.value,
                    filled_qty=ZERO,
                    avg_fill_price=ZERO,
                    fee_quote=ZERO,
                    mode=intent.mode.value,
                    is_simulated=intent.mode is not Mode.LIVE,
                    created_at=now or utcnow(),
                )
            )
            await session.flush()

    async def update_order_status(self, order_id: str, status: OrderStatus, *, note: str) -> None:
        """Record a local status change after a cancel was acknowledged."""

        async with self.db.session() as session:
            row = await session.get(OrderRow, order_id)
            if row is None:
                raise ValueError(f"order {order_id} not found")
            row.status = status.value
            session.add(
                OrderEventRow(
                    order_id=order_id,
                    event_type=status.value,
                    payload_json={"note": note},
                )
            )
            await session.flush()

    async def has_unsettled_close_for(
        self, account_id: str, symbol: str, mode: str
    ) -> bool:
        """A close order on this account+symbol+route that may still fill."""

        async with self.db.session() as session:
            row = (
                await session.execute(
                    select(OrderRow)
                    .join(TradeIntentRow, TradeIntentRow.id == OrderRow.intent_id)
                    .where(
                        TradeIntentRow.account_id == account_id,
                        TradeIntentRow.mode == mode,
                        OrderRow.symbol == symbol,
                        OrderRow.side == "sell",
                        OrderRow.status.in_(
                            [
                                OrderStatus.ACCEPTED.value,
                                OrderStatus.PARTIALLY_FILLED.value,
                                OrderStatus.UNKNOWN.value,
                            ]
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row is not None

    async def latest_order_for_signal(self, signal_id: str) -> OrderRow | None:
        async with self.db.session() as session:
            return (
                await session.execute(
                    select(OrderRow)
                    .join(TradeIntentRow, TradeIntentRow.id == OrderRow.intent_id)
                    .where(TradeIntentRow.signal_id == signal_id)
                    .order_by(OrderRow.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def get_order_for_intent(self, intent_id: str) -> OrderRow | None:
        async with self.db.session() as session:
            return (
                await session.execute(
                    select(OrderRow)
                    .where(OrderRow.intent_id == intent_id)
                    .order_by(OrderRow.created_at.desc())
                    .limit(1)
                )
            ).scalars().first()

    async def settle_reconciled_close(
        self,
        position_id: str,
        *,
        qty: Decimal,
        realized_pnl_usdt: Decimal,
        exit_order_id: str,
        closed: bool,
        now: datetime | None = None,
    ) -> None:
        """Apply a venue-confirmed close to the position row.

        Cash is handled by :meth:`adjust_account_cash`; this only records what
        is left of the position after the confirmed quantity.
        """

        async with self.db.session() as session:
            row = await session.get(PositionRow, position_id)
            if row is None:
                raise ValueError(f"position {position_id} not found")
            row.qty = max(qty, ZERO)
            row.realized_pnl_usdt = realized_pnl_usdt
            row.exit_order_id = exit_order_id
            if closed:
                row.qty = ZERO
                row.status = PositionStatus.CLOSED.value
                row.closed_at = now or utcnow()
            await session.flush()

    async def latest_close_order_for_signal(self, signal_id: str) -> OrderRow | None:
        """The newest order placed by a CLOSE intent of this signal."""

        async with self.db.session() as session:
            return (
                await session.execute(
                    select(OrderRow)
                    .join(TradeIntentRow, TradeIntentRow.id == OrderRow.intent_id)
                    .where(
                        TradeIntentRow.signal_id == signal_id,
                        TradeIntentRow.intent_type == IntentType.CLOSE.value,
                    )
                    .order_by(OrderRow.created_at.desc())
                    .limit(1)
                )
            ).scalars().first()

    async def latest_market_marks(self) -> dict[str, Decimal]:
        from sqlalchemy import and_

        async with self.db.session() as session:
            latest = (
                select(
                    MarketSnapshotRow.symbol,
                    func.max(MarketSnapshotRow.as_of).label("as_of"),
                )
                .group_by(MarketSnapshotRow.symbol)
                .subquery()
            )
            rows = list(
                (
                    await session.execute(
                        select(MarketSnapshotRow).join(
                            latest,
                            and_(
                                MarketSnapshotRow.symbol == latest.c.symbol,
                                MarketSnapshotRow.as_of == latest.c.as_of,
                            ),
                        )
                    )
                ).scalars()
            )
            return {
                row.symbol: (row.bid + row.ask) / Decimal("2") for row in rows
            }
