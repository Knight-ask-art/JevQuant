"""The per-cycle trading orchestrator.

One cycle:
  market snapshots -> policy (if needed) -> Jev candidates -> gate ->
  TradeIntent -> deterministic risk -> execution -> ledger -> exits -> PnL

Failures block *new entries* asymmetrically; existing positions keep their
deterministic exit path whenever execution remains healthy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..domain.enums import (
    IntentType,
    Mode,
    OrderPreference,
    OrderStatus,
    Side,
    SignalState,
)
from ..domain.enums import ErrorCode
from ..domain.errors import AppError, ReconciliationRequiredError
from ..domain.intent import ApprovedTradeIntent, Provenance, TradeIntent
from ..domain.market import AccountSnapshot, MarketSnapshot
from ..domain.money import ZERO, canonical_str
from ..domain.policy import EffectivePolicy
from ..domain.risk import RiskContext, RiskEngine, compute_risk_snapshot_hash
from ..storage.models import StrategyPolicyRow
from ..storage.repository import Repository
from .agent_tools import AgentToolbox
from .audit import AuditService
from .events import EventBroker
from .market_service import MarketService
from .modes import ModeManager
from .policy_service import PolicyService

DEFAULT_INTENT_TTL_SECONDS = 15

# A close order left working for longer than this is cancelled by the exit
# manager, measured in wall-clock seconds from the order's creation time.
CLOSE_ORDER_WORKING_LIMIT_SECONDS = 300


class CycleResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cycle_id: str
    started_at: datetime
    finished_at: datetime
    mode: str
    policy_id: str | None = None
    policy_generated: bool = False
    entries_blocked_reason: str | None = None
    symbols: list[str] = Field(default_factory=list)
    signals_created: int = 0
    signals_eligible: int = 0
    intents_created: int = 0
    approved: int = 0
    rejected: int = 0
    executed: int = 0
    exits: int = 0
    errors: int = 0
    simulated: bool = False
    equity_usdt: str = "0"
    open_positions: int = 0
    notes: list[str] = Field(default_factory=list)


class TradingOrchestrator:
    def __init__(
        self,
        *,
        settings,
        repo: Repository,
        policy_service: PolicyService,
        market: MarketService,
        jev,
        policy_generator,
        risk_engine: RiskEngine,
        execution_for_mode: dict[Mode, Any],
        modes: ModeManager,
        audit: AuditService,
        broker: EventBroker,
        account_id: str,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.policy_service = policy_service
        self.market = market
        self.jev = jev
        self.policy_generator = policy_generator
        self.risk_engine = risk_engine
        self.execution_for_mode = execution_for_mode
        self.modes = modes
        self.audit = audit
        self.broker = broker
        self.account_id = account_id

    async def request_close(self, position_id: str, *, actor: str) -> Any:
        """Mark an open position for an immediate close. Returns the row."""

        position = await self.repo.request_close(
            position_id, reason="manual", actor=actor
        )
        if position is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                "That position is not open.",
                details={"position_id": position_id},
            )
        await self.audit.record(
            "position.close_requested",
            "position",
            position_id,
            {"actor": actor, "symbol": position.symbol},
        )
        await self.broker.publish(
            "system.events",
            {"event": "position.close_requested", "position_id": position_id},
        )
        return position

    async def run_cycle(self, *, refresh_policy: bool = False) -> CycleResult:
        cycle_id = str(uuid4())
        started = datetime.now(UTC)
        mode = await self.modes.current_mode()
        kill = await self.modes.kill_switch_active()
        await self.repo.expire_policies(started)

        policy = await self.policy_service.get_active(mode.value)
        policy_row = await self.policy_service.get_active_row() if policy else None
        positions = await self.repo.get_open_positions(self.account_id)

        symbols: set[str] = set(self.settings.allowed_symbols)
        if policy is not None:
            symbols = {item.symbol for item in policy.universe}
        symbols |= {position.symbol for position in positions}

        feed_error: AppError | None = None
        snapshots: list[MarketSnapshot] = []
        try:
            snapshots = await self.market.snapshots(sorted(symbols), started)
        except AppError as exc:
            feed_error = exc

        snapshot_map: dict[str, MarketSnapshot] = {}
        for snapshot in snapshots:
            await self.repo.insert_market_snapshot(snapshot)
            snapshot_map[snapshot.symbol] = snapshot
        marks = {symbol: snap.mid for symbol, snap in snapshot_map.items()}
        account = await self.repo.account_snapshot(self.account_id, marks, started)

        now = datetime.now(UTC)
        policy_generated = False
        if not kill and (policy is None or refresh_policy):
            try:
                policy = await self._generate_policy(
                    mode, snapshots, account, cycle_id=cycle_id
                )
                policy_row = await self.policy_service.get_active_row()
                policy_generated = True
            except AppError as exc:
                await self.audit.record(
                    "cycle.policy_unavailable",
                    "cycle",
                    cycle_id,
                    {"code": exc.code.value, "message": exc.message},
                )

        adapter = self.execution_for_mode.get(mode)
        execution_health = False
        if adapter is not None:
            execution_health = bool((await adapter.health()).get("healthy"))
        feed_healthy = feed_error is None and bool(snapshots)
        system = await self.modes.system_snapshot(
            feed_healthy=feed_healthy,
            execution_healthy=execution_health,
            policy_fresh=policy is not None,
        )

        counters = {
            "signals_created": 0,
            "signals_eligible": 0,
            "intents_created": 0,
            "approved": 0,
            "rejected": 0,
            "executed": 0,
            "exits": 0,
            "errors": 0,
        }
        notes: list[str] = []
        blocked_reason: str | None = None
        if kill:
            blocked_reason = "kill_switch"
        elif policy is None:
            blocked_reason = "policy_unavailable"
        elif not feed_healthy:
            blocked_reason = "market_feed_unhealthy"
        elif not execution_health:
            blocked_reason = "execution_unhealthy"

        if blocked_reason is None and policy is not None:
            for symbol_policy in policy.universe:
                snapshot = snapshot_map.get(symbol_policy.symbol)
                if snapshot is None:
                    continue
                await self._process_entry(
                    mode=mode,
                    policy=policy,
                    policy_row=policy_row,
                    symbol_policy=symbol_policy,
                    snapshot=snapshot,
                    snapshot_map=snapshot_map,
                    system=system,
                    adapter=adapter,
                    counters=counters,
                    notes=notes,
                )
        else:
            await self.audit.record(
                "cycle.entries_blocked",
                "cycle",
                cycle_id,
                {"reason": blocked_reason, "mode": mode.value},
            )

        exit_count, exit_notes = await self._manage_exits(
            policy=policy,
            snapshot_map=snapshot_map,
            system=system,
        )
        counters["exits"] = exit_count
        notes.extend(exit_notes)

        final_account = await self.repo.account_snapshot(self.account_id, marks, started)
        account_row = await self.repo.get_account(self.account_id)
        positions_after = await self.repo.get_open_positions(self.account_id)
        unrealized = ZERO
        for position in positions_after:
            mark = marks.get(position.symbol, position.avg_entry_price)
            unrealized += position.qty * (mark - position.avg_entry_price)
        realized = account_row.realized_pnl_usdt if account_row else ZERO
        daily = account_row.daily_realized_pnl_usdt if account_row else ZERO
        await self.repo.insert_pnl_snapshot(
            account_id=self.account_id,
            as_of=started,
            equity_usdt=final_account.equity_usdt,
            cash_quote_usdt=final_account.available_quote_usdt,
            realized_pnl_usdt=realized,
            unrealized_pnl_usdt=unrealized,
            gross_exposure_usdt=final_account.gross_exposure_usdt,
            daily_realized_pnl_usdt=daily,
            drawdown_pct=final_account.drawdown_pct,
        )

        finished = datetime.now(UTC)
        finished_result = CycleResult(
            cycle_id=cycle_id,
            started_at=started,
            finished_at=finished,
            mode=mode.value,
            policy_id=str(policy.policy_id) if policy else None,
            policy_generated=policy_generated,
            entries_blocked_reason=blocked_reason,
            symbols=sorted(snapshot_map),
            simulated=any(snap.is_synthetic for snap in snapshots),
            equity_usdt=canonical_str(final_account.equity_usdt),
            open_positions=final_account.open_positions,
            notes=notes,
            **counters,
        )
        await self.audit.record(
            "cycle.completed",
            "cycle",
            cycle_id,
            finished_result.model_dump(mode="json"),
        )
        await self.broker.publish(
            "system.events",
            {"event": "cycle.completed", **finished_result.model_dump(mode="json")},
        )
        return finished_result

    async def _generate_policy(
        self,
        mode: Mode,
        snapshots: list[MarketSnapshot],
        account: AccountSnapshot,
        *,
        cycle_id: str | None = None,
    ) -> EffectivePolicy:
        now = datetime.now(UTC)
        recent: dict[str, list[dict[str, float]]] = {}
        for snap in snapshots:
            _snap, bars = await self._entry_snapshot(snap, now)
            recent[snap.symbol] = bars
        market_summary = {
            "universe": sorted({snap.symbol for snap in snapshots}),
            "timeframes": ["1m", "5m"],
            "as_of": now.isoformat(),
            "selection": (
                "You choose which symbols from the universe to trade. A symbol "
                "outside the universe is discarded by the server compiler."
            ),
            "symbols": {
                snap.symbol: {
                    "snapshot_id": str(snap.snapshot_id),
                    "mid": float(snap.mid),
                    "spread_bps": float(snap.spread_bps),
                    **snap.features,
                }
                for snap in snapshots
            },
            "recent_candles_1m": recent,
            "data_source": self.market.source,
            "simulated": any(snap.is_synthetic for snap in snapshots),
        }
        account_state = {
            "equity_usdt": float(account.equity_usdt),
            "available_quote_usdt": float(account.available_quote_usdt),
            "open_positions": account.open_positions,
            "daily_realized_pnl_pct": float(account.daily_realized_pnl_pct),
        }
        purpose = None
        if getattr(self.policy_generator, "provider", "") == "opencode_console":
            purpose = "trade_runtime"
        toolbox = AgentToolbox(
            repo=self.repo,
            market=self.market,
            account_id=self.account_id,
            mode=mode.value,
            symbols=self.settings.allowed_symbols,
            snapshots=snapshots,
            now=now,
        )
        effective, _row = await self.policy_service.generate(
            generator=self.policy_generator,
            mode=mode.value,
            account_state=account_state,
            market_summary=market_summary,
            purpose=purpose,
            toolbox=toolbox,
            cycle_id=cycle_id,
        )
        return effective

    async def _entry_snapshot(
        self, snapshot: MarketSnapshot, now: datetime
    ) -> tuple[MarketSnapshot, list[dict[str, float]]]:
        """Add recent 1m bars to the state the models receive.

        The snapshot already carries indicators; a strategy author that also
        sees the candles can describe an entry style in terms of actual price
        action rather than a single tick. A failure to fetch them is not fatal:
        the features and mid are still a complete, honest description.
        """

        try:
            candles = await self.market.klines(snapshot.symbol, interval="1m", limit=30)
        except AppError:
            return snapshot, []
        return snapshot, [
            {
                "t": candle.close_time.isoformat(),
                "o": candle.open,
                "h": candle.high,
                "l": candle.low,
                "c": candle.close,
                "v": candle.volume,
            }
            for candle in candles
        ]

    def _jev_state(
        self,
        policy: EffectivePolicy,
        symbol_policy,
        snapshot: MarketSnapshot,
        now: datetime,
        recent: list[dict[str, float]] | None = None,
    ) -> dict[str, Any]:
        return {
            "symbol": snapshot.symbol,
            "timestamp": now.isoformat(),
            "candle_close_at": snapshot.candle_close_at.isoformat(),
            "policy_id": str(policy.policy_id),
            "mid": float(snapshot.mid),
            "bid": float(snapshot.bid),
            "ask": float(snapshot.ask),
            "spread_bps": float(snapshot.spread_bps),
            **snapshot.features,
            "recent_candles_1m": recent or [],
            "policy": {
                "allowed_sides": [side.value for side in symbol_policy.allowed_sides],
                "max_spread_bps": float(symbol_policy.max_spread_bps),
                "max_position_notional_usdt": float(
                    symbol_policy.max_position_notional_usdt
                ),
                "entry_style": policy.regime.notes or policy.regime.label.value,
                "regime": policy.regime.label.value,
            },
        }

    def _gate(self, policy: EffectivePolicy, jev_result) -> tuple[bool, dict[str, Any]]:
        gate = policy.jev_gate
        probability = jev_result.entry_probability
        details = {
            "entry_choice": jev_result.entry_choice,
            "entry_probability": probability,
            "policy_fit": jev_result.policy_fit.noul,
            "quality_score": jev_result.entry_quality.score,
            "required": {
                "entry_choice": gate.entry_choice.value,
                "min_choice_probability": gate.min_choice_probability,
                "min_policy_fit": gate.min_policy_fit,
                "min_quality_score": gate.min_quality_score,
            },
        }
        passed = (
            jev_result.entry_choice == gate.entry_choice.value
            and probability >= gate.min_choice_probability
            and jev_result.policy_fit.noul >= gate.min_policy_fit
            and jev_result.entry_quality.score >= gate.min_quality_score
        )
        return passed, details

    def _entry_limit_price(self, snapshot: MarketSnapshot, rules):
        slippage = self.settings.slippage_bps / Decimal("10000")
        price = rules.quantize_price(snapshot.ask * (Decimal("1") + slippage))
        if price < snapshot.ask:
            price += rules.tick_size
        return min(price, rules.max_price)

    async def _process_entry(
        self,
        *,
        mode: Mode,
        policy: EffectivePolicy,
        policy_row: StrategyPolicyRow | None,
        symbol_policy,
        snapshot: MarketSnapshot,
        snapshot_map: dict[str, MarketSnapshot],
        system,
        adapter,
        counters: dict[str, int],
        notes: list[str],
    ) -> None:
        now = datetime.now(UTC)
        symbol = symbol_policy.symbol
        signal = await self.repo.create_signal(
            symbol=symbol,
            policy_id=str(policy.policy_id),
            snapshot_id=str(snapshot.snapshot_id),
            side=Side.BUY.value,
            detail={"source": snapshot.source, "simulated": snapshot.is_synthetic},
        )
        counters["signals_created"] += 1
        await self.broker.publish(
            "signal.candidates",
            {
                "signal_id": signal.id,
                "symbol": symbol,
                "state": SignalState.CANDIDATE.value,
                "snapshot_id": str(snapshot.snapshot_id),
            },
        )
        await self.repo.transition_signal(signal.id, SignalState.JEV_EVALUATING)
        snapshot, recent = await self._entry_snapshot(snapshot, now)
        state = self._jev_state(policy, symbol_policy, snapshot, now, recent)
        from ..adapters.models.base import build_jev_questions

        questions = build_jev_questions()
        try:
            jev_result = await self.jev.evaluate(state, questions, signal_id=signal.id)
        except AppError as exc:
            await self.repo.insert_jev_evaluation(
                signal_id=signal.id,
                model=getattr(self.jev, "model_name", "unknown"),
                request={"state": state, "questions": questions},
                response={},
                answers={},
                error_code=exc.code.value,
            )
            await self.repo.transition_signal(
                signal.id, SignalState.ERROR, {"reason": exc.code.value}
            )
            counters["errors"] += 1
            notes.append(f"{symbol}: jev error {exc.code.value}")
            await self.audit.record(
                "jev.error",
                "signal",
                signal.id,
                {"symbol": symbol, "code": exc.code.value},
            )
            return

        evaluation = await self.repo.insert_jev_evaluation(
            signal_id=signal.id,
            model=jev_result.model,
            request={"state": state, "questions": questions},
            response=jev_result.raw,
            answers={
                "entry_action": jev_result.entry_action.model_dump(mode="json"),
                "entry_quality": jev_result.entry_quality.model_dump(mode="json"),
                "policy_fit": jev_result.policy_fit.model_dump(mode="json"),
            },
            latency_ms=jev_result.latency_ms,
        )
        await self.repo.attach_jev_evaluation(
            signal.id,
            evaluation.id,
            score={
                "entry_choice": jev_result.entry_choice,
                "entry_probability": jev_result.entry_probability,
                "policy_fit": jev_result.policy_fit.noul,
                "quality_score": jev_result.entry_quality.score,
                "simulated": jev_result.simulated,
                "model": jev_result.model,
            },
        )
        await self.repo.transition_signal(
            signal.id,
            SignalState.SCORED,
            {"jev_evaluation_id": evaluation.id},
        )

        passed, gate_detail = self._gate(policy, jev_result)
        if not passed:
            await self.repo.transition_signal(signal.id, SignalState.REJECTED, gate_detail)
            counters["rejected"] += 1
            await self.audit.record(
                "signal.rejected",
                "signal",
                signal.id,
                {"symbol": symbol, "stage": "jev_gate", **gate_detail},
            )
            await self.broker.publish(
                "signal.candidates",
                {
                    "signal_id": signal.id,
                    "symbol": symbol,
                    "state": SignalState.REJECTED.value,
                    "detail": gate_detail,
                },
            )
            return

        await self.repo.transition_signal(signal.id, SignalState.ELIGIBLE, gate_detail)
        counters["signals_eligible"] += 1
        await self._prepare_and_execute_entry(
            mode=mode,
            policy=policy,
            policy_row=policy_row,
            symbol_policy=symbol_policy,
            snapshot=snapshot,
            snapshot_map=snapshot_map,
            system=system,
            adapter=adapter,
            signal_id=signal.id,
            jev_result=jev_result,
            jev_evaluation_id=evaluation.id,
            counters=counters,
            notes=notes,
        )

    async def _prepare_and_execute_entry(
        self,
        *,
        mode: Mode,
        policy: EffectivePolicy,
        policy_row: StrategyPolicyRow | None,
        symbol_policy,
        snapshot: MarketSnapshot,
        snapshot_map: dict[str, MarketSnapshot],
        system,
        adapter,
        signal_id: str,
        jev_result,
        jev_evaluation_id: str,
        counters: dict[str, int],
        notes: list[str],
    ) -> None:
        now = datetime.now(UTC)
        symbol = symbol_policy.symbol
        marks = {key: value.mid for key, value in snapshot_map.items()}
        try:
            rules = await self.market.entry_filters(symbol)
            account = await self.repo.account_snapshot(self.account_id, marks, now)
            position = await self.repo.get_open_position(self.account_id, symbol)
            last_entry = await self.repo.last_entry_at(self.account_id, symbol)
        except Exception as exc:  # noqa: BLE001
            await self.repo.transition_signal(
                signal_id, SignalState.EXPIRED, {"reason": type(exc).__name__}
            )
            counters["errors"] += 1
            notes.append(f"{symbol}: preparation failed ({type(exc).__name__})")
            return

        idempotency_key = (
            f"intent-{str(policy.policy_id)[:8]}-{symbol}-"
            f"{int(snapshot.candle_close_at.timestamp())}-buy"
        )
        existing = await self.repo.get_intent_by_idempotency(idempotency_key)
        if existing is not None:
            await self.repo.transition_signal(
                signal_id,
                SignalState.EXPIRED,
                {"reason": "duplicate_idempotency_key", "intent_id": existing.id},
            )
            notes.append(f"{symbol}: duplicate intent suppressed")
            return

        budget = account.available_quote_usdt * Decimal("0.25")
        quote_notional = min(symbol_policy.max_position_notional_usdt, budget)
        if quote_notional < rules.min_notional:
            await self.repo.transition_signal(
                signal_id,
                SignalState.EXPIRED,
                {"reason": "notional_below_exchange_minimum"},
            )
            notes.append(f"{symbol}: sizing below exchange minimum")
            return

        limit_price = self._entry_limit_price(snapshot, rules)
        expires_at = now + timedelta(seconds=DEFAULT_INTENT_TTL_SECONDS)
        provenance = Provenance(
            llm_model=policy_row.source if policy_row else "unknown",
            jev_model=jev_result.model,
            jev_evaluation_id=jev_evaluation_id,
            policy_source=policy_row.source if policy_row else "unknown",
            data_source=snapshot.source,
            simulated=bool(
                (policy_row.is_simulated if policy_row else False)
                or jev_result.simulated
                or snapshot.is_synthetic
            ),
        )
        provisional = TradeIntent(
            intent_id=uuid4(),
            signal_id=UUID(signal_id),
            policy_id=policy.policy_id,
            mode=mode,
            account_id=UUID(self.account_id),
            symbol=symbol,
            side=Side.BUY,
            intent_type=IntentType.OPEN,
            order_preference=OrderPreference.LIMIT,
            quote_notional=quote_notional,
            max_slippage_bps=min(
                self.settings.slippage_bps, symbol_policy.max_slippage_bps
            ),
            limit_price=limit_price,
            stop_loss_bps=policy.exit_defaults.stop_loss_bps,
            take_profit_bps=policy.exit_defaults.take_profit_bps,
            max_holding_seconds=policy.exit_defaults.max_holding_seconds,
            created_at=now,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
            risk_snapshot_hash="pending",
            provenance=provenance,
        )
        context = RiskContext(
            policy=policy,
            account=account,
            market=snapshot,
            symbol_rules=rules,
            system=system,
            now=now,
            position_qty=position.qty if position is not None else ZERO,
            last_entry_at=last_entry,
            max_staleness_ms=self.settings.market_staleness_ms,
            exit_slippage_bps=self.settings.exit_slippage_bps,
        )
        risk_hash = compute_risk_snapshot_hash(provisional, context)
        intent = provisional.model_copy(update={"risk_snapshot_hash": risk_hash})

        _row, created = await self.repo.insert_intent(intent, status="RISK_PENDING")
        if not created:
            await self.repo.transition_signal(
                signal_id, SignalState.EXPIRED, {"reason": "idempotency_conflict"}
            )
            notes.append(f"{symbol}: idempotency conflict suppressed")
            return
        counters["intents_created"] += 1
        await self.repo.transition_signal(
            signal_id, SignalState.INTENT_CREATED, {"intent_id": str(intent.intent_id)}
        )
        await self.repo.transition_signal(signal_id, SignalState.RISK_PENDING)

        decision = self.risk_engine.evaluate(intent, context)
        await self.repo.insert_risk_decision(decision)
        await self.audit.record(
            "risk.decision",
            "intent",
            str(intent.intent_id),
            {
                "approved": decision.approved,
                "failed_rules": [failure.model_dump(mode="json") for failure in decision.failed_rules],
                "effective": decision.effective,
                "decision_hash": decision.decision_hash,
            },
        )
        if not decision.approved:
            await self.repo.transition_signal(
                signal_id,
                SignalState.RISK_REJECTED,
                {"rules": decision.failed_rule_names},
            )
            counters["rejected"] += 1
            notes.append(f"{symbol}: risk rejected ({','.join(decision.failed_rule_names)})")
            return

        await self.repo.transition_signal(
            signal_id, SignalState.APPROVED, {"decision_id": str(decision.decision_id)}
        )
        counters["approved"] += 1
        await self.broker.publish(
            "trade.intents.approved",
            {
                "intent_id": str(intent.intent_id),
                "symbol": symbol,
                "notional": canonical_str(decision.approved_intent.effective_quote_notional)
                if decision.approved_intent
                else "",
            },
        )
        await self.repo.transition_signal(signal_id, SignalState.EXECUTION_PENDING)
        try:
            receipt = await adapter.submit(decision.approved_intent, snapshot, rules)
        except ReconciliationRequiredError as exc:
            await self.repo.transition_signal(
                signal_id,
                SignalState.RECONCILIATION_REQUIRED,
                {"reason": exc.code.value},
            )
            counters["errors"] += 1
            notes.append(f"{symbol}: execution outcome unknown; reconciliation required")
            await self.audit.record(
                "execution.reconciliation_required", "intent", str(intent.intent_id), {}
            )
            return
        except AppError as exc:
            await self.repo.transition_signal(
                signal_id, SignalState.ERROR, {"reason": exc.code.value}
            )
            counters["errors"] += 1
            notes.append(f"{symbol}: execution error {exc.code.value}")
            return
        await self._apply_receipt(signal_id, receipt, counters, notes)

    async def dispatch_approved_intent(self, intent_id: str) -> dict[str, Any]:
        """Dispatch an already-approved intent through the active adapter.

        This is the internal execution-gateway entry point: the control plane
        creates and approves intents, the gateway performs the exchange call.
        Only intents whose risk decision is approved and whose mode matches the
        active mode can be dispatched, and a dispatch is attempted once - a
        retry requires a fresh intent with a new idempotency key.
        """

        row = await self.repo.get_intent(intent_id)
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, f"Intent {intent_id} was not found.")
        if row.status != "APPROVED":
            raise AppError(
                ErrorCode.CONFLICT,
                f"Intent {intent_id} is in status {row.status}; only APPROVED "
                "intents can be dispatched.",
            )
        decision_row = await self.repo.get_risk_decision_for_intent(intent_id)
        if (
            decision_row is None
            or not decision_row.approved
            or not decision_row.approved_intent_json
        ):
            raise AppError(
                ErrorCode.CONFLICT,
                f"Intent {intent_id} has no approved risk decision.",
            )
        approved = ApprovedTradeIntent.model_validate(decision_row.approved_intent_json)
        now = datetime.now(UTC)
        if approved.intent.expires_at <= now:
            await self.repo.update_intent_status(intent_id, "EXPIRED")
            raise AppError(
                ErrorCode.POLICY_EXPIRED,
                f"Intent {intent_id} expired at {approved.intent.expires_at.isoformat()}.",
            )
        mode = await self.modes.current_mode()
        if approved.intent.mode is not mode:
            raise AppError(
                ErrorCode.CONFLICT,
                f"Intent mode {approved.intent.mode.value} does not match the "
                f"active mode {mode.value}.",
            )
        adapter = self.execution_for_mode.get(mode)
        if adapter is None:
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                f"No execution adapter is configured for mode {mode.value}.",
            )
        if not bool((await adapter.health()).get("healthy")):
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                f"The {mode.value} execution backend is unhealthy.",
            )
        signal = await self.repo.get_signal(row.signal_id)
        if signal is None:
            raise AppError(
                ErrorCode.NOT_FOUND, f"Signal {row.signal_id} was not found."
            )
        if signal.state == SignalState.APPROVED.value:
            await self.repo.transition_signal(row.signal_id, SignalState.EXECUTION_PENDING)
        elif signal.state != SignalState.EXECUTION_PENDING.value:
            raise AppError(
                ErrorCode.CONFLICT,
                f"Signal {row.signal_id} is in state {signal.state}; dispatch "
                "requires APPROVED or EXECUTION_PENDING.",
            )

        symbol = approved.intent.symbol
        snapshots = await self.market.snapshots([symbol], now)
        if not snapshots:
            raise AppError(
                ErrorCode.STALE_MARKET_DATA, f"No fresh market snapshot for {symbol}."
            )
        snapshot = snapshots[0]
        await self.repo.insert_market_snapshot(snapshot)
        rules = await self.market.filters(symbol)
        await self.repo.update_intent_status(intent_id, "EXECUTION_PENDING")
        counters = {"executed": 0}
        notes: list[str] = []
        try:
            receipt = await adapter.submit(approved, snapshot, rules)
        except ReconciliationRequiredError as exc:
            await self.repo.transition_signal(
                row.signal_id,
                SignalState.RECONCILIATION_REQUIRED,
                {"reason": exc.code.value},
            )
            await self.repo.update_intent_status(intent_id, "RECONCILIATION_REQUIRED")
            await self.audit.record(
                "execution.reconciliation_required", "intent", intent_id, {}
            )
            return {
                "intent_id": intent_id,
                "status": SignalState.RECONCILIATION_REQUIRED.value,
                "order_id": None,
                "notes": ["execution outcome unknown; reconciliation required"],
            }
        except AppError as exc:
            # The adapter reported a definite failure: no order exists, so the
            # signal must not stay parked in EXECUTION_PENDING forever.
            await self.repo.transition_signal(
                row.signal_id, SignalState.ERROR, {"reason": exc.code.value}
            )
            await self.repo.update_intent_status(intent_id, "ERROR")
            await self.audit.record(
                "execution.error", "intent", intent_id, {"code": exc.code.value}
            )
            return {
                "intent_id": intent_id,
                "status": SignalState.ERROR.value,
                "order_id": None,
                "notes": [f"execution error {exc.code.value}"],
            }
        counters.setdefault("errors", 0)
        outcome = await self._apply_receipt(row.signal_id, receipt, counters, notes)
        # Accepted/partial fills may still complete; only a definite rejection
        # or error ends the intent here.
        final_status = {
            "FILLED": "EXECUTED",
            "PARTIALLY_FILLED": "EXECUTED",
            "ACCEPTED": "ACCEPTED",
            "CANCELLED": "CANCELLED",
            "UNKNOWN": "RECONCILIATION_REQUIRED",
        }.get(outcome, "ERROR")
        await self.repo.update_intent_status(intent_id, final_status)
        signal_after = await self.repo.get_signal(row.signal_id)
        return {
            "intent_id": intent_id,
            "status": signal_after.state if signal_after is not None else final_status,
            "intent_status": final_status,
            "order_id": receipt.order_id,
            "position_id": receipt.position_id,
            "filled_qty": canonical_str(receipt.filled_qty),
            "avg_fill_price": canonical_str(receipt.avg_fill_price),
            "notes": notes,
        }

    def _receipt_outcome(self, receipt) -> str:
        """Classify a receipt by the signal state it leads to."""

        if receipt.status is OrderStatus.FILLED:
            return "FILLED"
        if receipt.status is OrderStatus.PARTIALLY_FILLED:
            return "PARTIALLY_FILLED"
        if receipt.status in {OrderStatus.ACCEPTED, OrderStatus.SHADOWED}:
            return "ACCEPTED"
        if receipt.status is OrderStatus.CANCELLED:
            return "CANCELLED"
        if receipt.status is OrderStatus.UNKNOWN:
            return "UNKNOWN"
        return "ERROR"

    async def _apply_receipt(
        self, signal_id: str, receipt, counters: dict[str, int], notes: list[str]
    ) -> str:
        status = receipt.status
        outcome = self._receipt_outcome(receipt)
        try:
            if status is OrderStatus.FILLED:
                await self.repo.transition_signal(
                    signal_id, SignalState.ACCEPTED, {"order_id": receipt.order_id}
                )
                await self.repo.transition_signal(
                    signal_id, SignalState.FILLED, {"order_id": receipt.order_id}
                )
                if receipt.position_id:
                    await self.repo.transition_signal(
                        signal_id,
                        SignalState.POSITION_OPEN,
                        {"position_id": receipt.position_id},
                    )
                counters["executed"] += 1
            elif status is OrderStatus.PARTIALLY_FILLED:
                await self.repo.transition_signal(
                    signal_id, SignalState.ACCEPTED, {"order_id": receipt.order_id}
                )
                await self.repo.transition_signal(
                    signal_id,
                    SignalState.PARTIALLY_FILLED,
                    {"filled_qty": canonical_str(receipt.filled_qty)},
                )
                counters["executed"] += 1
            elif status in {OrderStatus.ACCEPTED, OrderStatus.SHADOWED}:
                await self.repo.transition_signal(
                    signal_id,
                    SignalState.ACCEPTED,
                    {
                        "order_id": receipt.order_id,
                        "shadowed": status is OrderStatus.SHADOWED,
                    },
                )
            elif status is OrderStatus.CANCELLED:
                await self.repo.transition_signal(
                    signal_id, SignalState.ACCEPTED, {"order_id": receipt.order_id}
                )
                await self.repo.transition_signal(
                    signal_id, SignalState.CANCELLED, {"order_id": receipt.order_id}
                )
            elif status is OrderStatus.UNKNOWN:
                await self.repo.transition_signal(
                    signal_id,
                    SignalState.RECONCILIATION_REQUIRED,
                    {"reason": "unknown_order_status"},
                )
                counters["errors"] = counters.get("errors", 0) + 1
            else:
                await self.repo.transition_signal(
                    signal_id, SignalState.ERROR, {"reason": status.value}
                )
                counters["errors"] = counters.get("errors", 0) + 1
        except ValueError as exc:
            notes.append(f"receipt transition skipped: {exc}")
        await self.audit.record(
            "execution.receipt",
            "signal",
            signal_id,
            receipt.model_dump(mode="json"),
        )
        await self.broker.publish(
            "execution.events",
            {
                "signal_id": signal_id,
                "order_id": receipt.order_id,
                "status": receipt.status.value,
                "filled_qty": canonical_str(receipt.filled_qty),
                "avg_fill_price": canonical_str(receipt.avg_fill_price),
            },
        )
        return outcome

    async def _now(self) -> datetime:
        """Wall-clock now. Isolated so tests can pin it."""

        return datetime.now(UTC)

    @staticmethod
    def _mode_of(position) -> Mode:
        try:
            return Mode(getattr(position, "mode", None) or Mode.PAPER.value)
        except ValueError:
            return Mode.PAPER

    async def _resolve_pending_close(
        self, position, signal, adapter, now: datetime, notes: list[str]
    ) -> int:
        """Finish or back out a close order that is still EXIT_PENDING.

        The exit manager used to skip EXIT_PENDING signals outright, so a
        resting close (or any non-AppError) left the position permanently
        unmanaged. This reconciles the working close with its recorded order:

        * filled        -> CLOSED
        * rejected/error -> POSITION_OPEN (a fresh exit may be attempted)
        * still working  -> cancelled once older than the working limit, then
                            POSITION_OPEN once the cancel is acknowledged
        """

        # Resolve against the close order of *this* signal, not "whatever order
        # this signal has" - the entry order is not a close.
        intent = await self.repo.get_signal_intent(signal.id, intent_type=IntentType.CLOSE)
        order = await self.repo.latest_close_order_for_signal(signal.id)
        state_note = {
            "symbol": position.symbol,
            "intent_id": intent.id if intent else None,
            "order_status": order.status if order else None,
        }
        if order is None:
            notes.append(f"{position.symbol}: exit pending without an order; manual review")
            return 0

        if order.status == OrderStatus.FILLED.value:
            await self.repo.transition_signal(
                signal.id, SignalState.CLOSED, {**state_note, "source": "close_resolved"}
            )
            if intent is not None:
                await self.repo.update_intent_status(intent.id, "EXECUTED")
            await self.audit.record("exit.resolved", "position", position.id, state_note)
            return 1
        if order.status == OrderStatus.PARTIALLY_FILLED.value:
            notes.append(f"{position.symbol}: partial exit still working")
            return 0
        if order.status in {OrderStatus.REJECTED.value, "ERROR", "UNKNOWN"}:
            await self.repo.transition_signal(
                signal.id,
                SignalState.POSITION_OPEN,
                {**state_note, "reason": "close_rejected"},
            )
            if intent is not None:
                await self.repo.update_intent_status(intent.id, order.status)
            notes.append(f"{position.symbol}: exit rejected; position kept open")
            return 0

        created = order.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age = (now - created).total_seconds()
        if age < CLOSE_ORDER_WORKING_LIMIT_SECONDS:
            notes.append(f"{position.symbol}: exit order working")
            return 0
        try:
            await adapter.cancel(order.id)
        except ReconciliationRequiredError:
            await self.repo.transition_signal(
                signal.id,
                SignalState.RECONCILIATION_REQUIRED,
                {**state_note, "reason": "cancel_outcome_unknown"},
            )
            if intent is not None:
                await self.repo.update_intent_status(
                    intent.id, "RECONCILIATION_REQUIRED"
                )
            notes.append(f"{position.symbol}: exit cancel outcome unknown; reconcile")
            return 0
        except AppError as exc:
            notes.append(f"{position.symbol}: exit cancel failed ({exc.code.value})")
            return 0
        await self.repo.transition_signal(
            signal.id,
            SignalState.POSITION_OPEN,
            {**state_note, "reason": "close_cancelled_after_timeout"},
        )
        if intent is not None:
            await self.repo.update_intent_status(intent.id, "CANCELLED")
        await self.audit.record(
            "exit.cancelled", "position", position.id, {**state_note, "age_seconds": int(age)}
        )
        notes.append(f"{position.symbol}: stale exit order cancelled; retry next cycle")
        return 0

    async def _await_snapshot(self, symbol: str, now: datetime) -> MarketSnapshot | None:
        """The next snapshot for a symbol after price ``now``.

        With the live feed that is simply the latest snapshot. With the demo
        clock the provider yields one fresh closed candle per call, so asking
        again is the only way to see a different price without waiting.
        """

        snapshots = await self.market.snapshots([symbol], now)
        if not snapshots:
            return None
        snapshot = snapshots[0]
        await self.repo.insert_market_snapshot(snapshot)
        return snapshot

    async def _policy_for_position(self, position, current: EffectivePolicy | None):
        if current is not None and str(current.policy_id) == position.policy_id:
            return current
        row = await self.repo.get_policy_by_policy_id(position.policy_id)
        if row is None:
            return None
        return EffectivePolicy.model_validate(row.effective_json)

    def _exit_reason(self, position, snapshot: MarketSnapshot, now: datetime) -> str | None:
        entry = position.avg_entry_price
        stop_bps = position.stop_loss_bps or ZERO
        take_bps = position.take_profit_bps or ZERO
        if stop_bps > ZERO and snapshot.bid <= entry * (Decimal("1") - stop_bps / Decimal("10000")):
            return "stop_loss"
        if take_bps > ZERO and snapshot.bid >= entry * (Decimal("1") + take_bps / Decimal("10000")):
            return "take_profit"
        opened = position.opened_at
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=UTC)
        if position.max_holding_seconds and now >= opened + timedelta(
            seconds=position.max_holding_seconds
        ):
            return "time_limit"
        return None

    async def _exit_system_snapshot(self) -> SystemSnapshot:
        """The dependencies an exit reads, probed only while positions are open."""

        mode = await self.modes.current_mode()
        execution_healthy = False
        adapter = self.execution_for_mode.get(mode)
        if adapter is not None:
            execution_healthy = bool((await adapter.health()).get("healthy"))
        try:
            feed_healthy = bool((await self.market.health()).get("healthy"))
        except Exception:  # noqa: BLE001
            feed_healthy = False
        return await self.modes.system_snapshot(
            feed_healthy=feed_healthy,
            execution_healthy=execution_healthy,
            # The compiler's policy-fresh flag decides whether *entries* are
            # allowed; an exit must never be blocked by a stale strategy.
            policy_fresh=True,
        )

    async def _manage_exits(
        self,
        *,
        policy: EffectivePolicy | None,
        snapshot_map: dict[str, MarketSnapshot],
        system,
    ) -> tuple[int, list[str]]:
        """Route deterministic exits for every open position.

        Each position exits through the adapter of the mode that opened it, not
        the currently selected mode: after a restart (live resets to paper) or
        a mode change, a real exchange position must still be closed on the
        exchange that holds it.
        """

        now = await self._now()
        notes: list[str] = []
        exits = 0
        positions = await self.repo.get_open_positions(self.account_id)
        for position in positions:
            snapshot = snapshot_map.get(position.symbol)
            if snapshot is None:
                try:
                    snapshot = await self._await_snapshot(position.symbol, now)
                except AppError as exc:
                    notes.append(f"{position.symbol}: exit feed unavailable ({exc.code.value})")
                    continue
                if snapshot is not None:
                    snapshot_map[position.symbol] = snapshot
            if snapshot is None:
                continue

            position_mode = self._mode_of(position)
            adapter = self.execution_for_mode.get(position_mode)
            if adapter is None:
                notes.append(
                    f"{position.symbol}: no execution adapter for {position_mode.value}"
                )
                continue
            execute = position_mode is not await self.modes.current_mode()

            signal = await self.repo.get_signal(position.signal_id)
            if signal is None:
                notes.append(f"{position.symbol}: position has no signal; manual review")
                continue
            state = SignalState(signal.state)
            if state is SignalState.EXIT_PENDING:
                exits += await self._resolve_pending_close(
                    position, signal, adapter, now, notes
                )
                continue
            if state in {
                SignalState.RECONCILIATION_REQUIRED,
                SignalState.CLOSED,
                SignalState.ERROR,
                SignalState.CANCELLED,
                SignalState.EXPIRED,
            }:
                continue
            if state is SignalState.PARTIALLY_FILLED:
                await self.repo.transition_signal(
                    signal.id, SignalState.FILLED, {"reason": "position partial entry"}
                )
                await self.repo.transition_signal(
                    signal.id, SignalState.POSITION_OPEN, {"position_id": position.id}
                )
                state = SignalState.POSITION_OPEN
            if state is not SignalState.POSITION_OPEN:
                continue
            if execute and await self.modes.kill_switch_active():
                notes.append(f"{position.symbol}: exit deferred by kill switch on {position_mode.value}")
                continue

            reason = self._exit_reason(position, snapshot, now) or position.exit_requested_reason
            if reason is None:
                continue
            idempotency_suffix = f"{int(now.timestamp() // 60)}"
            if await self.repo.has_unsettled_close_for(
                self.account_id, position.symbol, position_mode.value
            ):
                # An earlier close of this symbol is still working. The demo
                # clock keeps the old minute key, so the idempotency check
                # below cannot see this new signal; give it its own key.
                idempotency_suffix = f"retry-{int(now.timestamp())}-{signal.id[:8]}"
            exit_policy = await self._policy_for_position(position, policy)
            if exit_policy is None:
                notes.append(f"{position.symbol}: exit policy unavailable")
                continue
            try:
                rules = await self.market.filters(position.symbol)
                marks = {position.symbol: snapshot.mid}
                account = await self.repo.account_snapshot(self.account_id, marks, now)
            except Exception as exc:  # noqa: BLE001
                notes.append(
                    f"{position.symbol}: exit preparation failed ({type(exc).__name__})"
                )
                continue

            idempotency_key = (
                f"close-{position.id[:8]}-{position.symbol}-{idempotency_suffix}-sell"
            )
            if await self.repo.get_intent_by_idempotency(idempotency_key) is not None:
                continue

            quote_notional = max(position.qty * snapshot.bid, rules.min_notional)
            slip = self.settings.exit_slippage_bps / Decimal("10000")
            limit_price = rules.quantize_price(snapshot.bid * (Decimal("1") - slip))
            if limit_price <= ZERO:
                limit_price = rules.quantize_price(snapshot.bid)
            if limit_price <= ZERO:
                notes.append(f"{position.symbol}: exit price rounds to zero")
                continue

            provisional = TradeIntent(
                intent_id=uuid4(),
                signal_id=UUID(signal.id),
                policy_id=exit_policy.policy_id,
                mode=position_mode,
                account_id=UUID(self.account_id),
                symbol=position.symbol,
                side=Side.SELL,
                intent_type=IntentType.CLOSE,
                order_preference=OrderPreference.LIMIT,
                quote_notional=quote_notional,
                max_slippage_bps=self.settings.exit_slippage_bps,
                limit_price=limit_price,
                stop_loss_bps=None,
                take_profit_bps=None,
                max_holding_seconds=None,
                created_at=now,
                expires_at=now + timedelta(seconds=DEFAULT_INTENT_TTL_SECONDS),
                idempotency_key=idempotency_key,
                risk_snapshot_hash="pending",
                provenance=Provenance(
                    llm_model="deterministic-exit",
                    policy_source="position",
                    data_source=snapshot.source,
                    simulated=snapshot.is_synthetic,
                ),
            )
            context = RiskContext(
                policy=exit_policy,
                account=account,
                market=snapshot,
                symbol_rules=rules,
                system=system,
                now=now,
                position_qty=position.qty,
                last_entry_at=None,
                max_staleness_ms=self.settings.market_staleness_ms,
                exit_slippage_bps=self.settings.exit_slippage_bps,
            )
            risk_hash = compute_risk_snapshot_hash(provisional, context)
            intent = provisional.model_copy(update={"risk_snapshot_hash": risk_hash})
            _row, created = await self.repo.insert_intent(intent, status="RISK_PENDING")
            if not created:
                continue
            decision = self.risk_engine.evaluate(intent, context)
            await self.repo.insert_risk_decision(decision)
            if not decision.approved:
                await self.audit.record(
                    "exit.rejected",
                    "position",
                    position.id,
                    {"reason": reason, "rules": decision.failed_rule_names},
                )
                notes.append(
                    f"{position.symbol}: exit blocked ({','.join(decision.failed_rule_names)})"
                )
                continue

            await self.repo.transition_signal(
                signal.id,
                SignalState.EXIT_PENDING,
                {"reason": reason, "intent_id": str(intent.intent_id)},
            )
            try:
                receipt = await adapter.submit(decision.approved_intent, snapshot, rules)
            except ReconciliationRequiredError:
                await self.repo.transition_signal(
                    signal.id,
                    SignalState.RECONCILIATION_REQUIRED,
                    {"reason": "exit_outcome_unknown"},
                )
                notes.append(f"{position.symbol}: exit outcome unknown; reconcile")
                continue
            except AppError as exc:
                await self.repo.transition_signal(
                    signal.id, SignalState.POSITION_OPEN, {"reason": exc.code.value}
                )
                notes.append(f"{position.symbol}: exit error {exc.code.value}")
                continue

            if receipt.status is OrderStatus.FILLED:
                await self.repo.transition_signal(
                    signal.id,
                    SignalState.CLOSED,
                    {"reason": reason, "order_id": receipt.order_id},
                )
                exits += 1
            elif receipt.status is OrderStatus.PARTIALLY_FILLED:
                notes.append(f"{position.symbol}: partial exit; position remains open")
                exits += 1
            elif receipt.status in {OrderStatus.ACCEPTED, OrderStatus.SHADOWED}:
                notes.append(f"{position.symbol}: exit order resting")
            elif receipt.status is OrderStatus.UNKNOWN:
                await self.repo.transition_signal(
                    signal.id,
                    SignalState.RECONCILIATION_REQUIRED,
                    {"reason": "unknown_exit_status"},
                )
            else:
                await self.repo.transition_signal(
                    signal.id,
                    SignalState.POSITION_OPEN,
                    {"reason": receipt.status.value},
                )
            await self.audit.record(
                "exit.executed",
                "position",
                position.id,
                {
                    "symbol": position.symbol,
                    "reason": reason,
                    "status": receipt.status.value,
                    "order_id": receipt.order_id,
                },
            )
            await self.broker.publish(
                "execution.events",
                {
                    "event": "exit",
                    "symbol": position.symbol,
                    "reason": reason,
                    "status": receipt.status.value,
                },
            )
        return exits, notes
