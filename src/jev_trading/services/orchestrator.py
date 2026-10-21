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
from ..domain.costs import CostModel
from ..domain.enums import ErrorCode, PositionStatus
from ..domain.errors import AppError, ReconciliationRequiredError, RiskRejectedError
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
from .edge_evaluator import EdgeEvaluator
from .regime_gate import RegimeGate

DEFAULT_INTENT_TTL_SECONDS = 15

# A close order left working for longer than this is cancelled by the exit
# manager, measured in wall-clock seconds from the order's creation time.
CLOSE_ORDER_WORKING_LIMIT_SECONDS = 300
# A close that still has not filled this long after generation is working
# against a market that has moved away from it. Waiting for the full working
# limit is how an exit quietly turns into a held loss: re-price instead.
RESTING_CLOSE_REPRICE_SECONDS = 60
# Signal states that mean an entry reached the market (the Jev gate let it
# through). The over-trading guard counts these, so a symbol Jev keeps
# rejecting is still evaluated as often as the cycle runs.
MARKET_REACHED_SIGNAL_STATES = tuple(
    state.value
    for state in SignalState
    if state
    in {
        SignalState.ELIGIBLE,
        SignalState.INTENT_CREATED,
        SignalState.RISK_PENDING,
        SignalState.RISK_REJECTED,
        SignalState.APPROVED,
        SignalState.EXECUTION_PENDING,
        SignalState.ACCEPTED,
        SignalState.PARTIALLY_FILLED,
        SignalState.FILLED,
        SignalState.POSITION_OPEN,
        SignalState.EXIT_PENDING,
        SignalState.CLOSED,
    }
)
# Signal states that mean a Jev call was made and paid for. The cadence keys
# on these: a rejection cost as much as a fill, so it must start the wait too.
# EXPIRED is excluded on purpose -- a signal expires before Jev is called
# (budget refusal, missing candle), and charging a full interval for a call
# that never happened would silently stop trading.
JEV_CONSUMED_SIGNAL_STATES = tuple(
    state.value
    for state in SignalState
    if state not in {SignalState.CANDIDATE, SignalState.EXPIRED}
)
# A resting sell is stale once the bid has risen this far above its limit: at
# that point it no longer tracks the market and is simply absorbing moves.
REPRICE_BPS = 10
# Staleness is measured on the snapshot's candle, not on the cycle clock: paper
# replays historical candles, and the wall clock would make every order look old.
POSITION_SNAPSHOT_GRACE_SECONDS = 60


def _needs_reprice(
    limit_price: Decimal | None, bid: Decimal, threshold_bps: int = REPRICE_BPS
) -> bool:
    """Whether a resting sell now sits above a bid the market has left behind.

    The order only fills when the bid rises to its limit. If the bid has
    dropped away from it, the exit is no longer tracking the market; chasing
    the bid is the only way the stop keeps its meaning.
    """

    if limit_price is None or limit_price <= ZERO or bid <= ZERO:
        return False
    return limit_price - bid >= limit_price * Decimal(threshold_bps) / 10_000


def _snapshot_key(snapshot: MarketSnapshot) -> tuple[str, int]:
    """Identity of the market view a decision was made against."""

    return (snapshot.symbol, int(snapshot.candle_close_at.timestamp()))


def _marketable_sell_price(bid: Decimal, rules) -> Decimal:
    """Sell limit just below the bid: crosses now, still legal on Binance spot.

    The exchange rejects a SELL limit at or above the bid, so the price is
    floored to tick size and dropped a step when it would sit on the bid.
    """

    price = rules.quantize_price(bid)
    if price >= bid:
        price -= rules.tick_size
    return rules.quantize_price(price)


def entry_notional(settings, symbol_policy, account, rules) -> Decimal:
    """What one entry may spend, or zero when that is below the exchange minimum.

    Two ceilings apply: the policy's per-symbol limit, and the server's share of
    available cash (``entry_budget_fraction``). The share is a server setting so
    no policy can risk more of the account than the operator allows, and on a
    small account it is what decides whether an entry is possible at all: a
    quarter of a 14 USDT account is 3.5, under Binance's 5 USDT minimum.
    """

    budget = account.available_quote_usdt * settings.entry_budget_fraction
    notional = min(symbol_policy.max_position_notional_usdt, budget)
    return notional if notional >= rules.min_notional else ZERO


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
    entries_blocked: int = 0
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
        budgets: Any | None = None,
        counterfactual: Any | None = None,
        regime_gate: Any | None = None,
        edge_evaluator: Any | None = None,
    ) -> None:
        self.settings = settings
        self.budgets = budgets
        self.counterfactual = counterfactual
        self.regime_gate = regime_gate or RegimeGate.from_settings(settings)
        self.cost_model = CostModel.from_settings(settings)
        self.edge_evaluator = edge_evaluator
        if self.edge_evaluator is None and getattr(settings, "edge_shadow_enabled", True):
            self.edge_evaluator = EdgeEvaluator.from_settings(settings, repo)
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
        # position id -> snapshot the last re-price was generated from.
        self._reprice_guard: dict[str, tuple[str, int]] = {}
        # position ids whose resting close was cancelled for a re-price most
        # recently; their replacement must not reuse this minute's key.
        self._repriced_positions: set[str] = set()
        # Closed-candle time of the last cycle, for the new-evidence check.
        self._last_candle_at: datetime | None = None

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
        candle_times: set[datetime] = set()
        for snapshot in snapshots:
            await self.repo.insert_market_snapshot(snapshot)
            snapshot_map[snapshot.symbol] = snapshot
            candle_times.add(snapshot.candle_close_at)
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
        # The candle the last cycle evaluated, so a repeat tick can be told
        # apart from a new one. Kept in memory: a restart always evaluates once.
        previous_candle = self._last_candle_at
        if feed_healthy and len(candle_times) == 1:
            self._last_candle_at = next(iter(candle_times))
        system = await self.modes.system_snapshot(
            feed_healthy=feed_healthy,
            execution_healthy=execution_health,
            policy_fresh=policy is not None,
        )

        counters = {
            "entries_blocked": 0,
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
        elif self.budgets is not None:
            # Checked once per cycle, before any signal row exists, so a
            # spent budget reads as a budget problem rather than as a run of
            # signals Jev never answered.
            try:
                await self.budgets.check_jev(started)
            except AppError as exc:
                blocked_reason = "jev_budget_exhausted"
                notes.append(f"Jev call budget spent: {exc.message}")
                await self.audit.record(
                    "cycle.budget_exhausted",
                    "cycle",
                    cycle_id,
                    {"provider": "jev", "code": exc.code.value, **exc.details},
                )

        if (
            self.settings.cycle_requires_new_candle
            and feed_healthy
            and len(candle_times) == 1
            and previous_candle == next(iter(candle_times))
        ):
            # Every symbol resolved to the candle already in hand. Replaying
            # it at the next tick re-asks Jev about evidence that has not
            # moved, so entries stop here; exits still run below. A cycle
            # blocked for a stronger reason keeps that reason.
            notes.append("no closed candle since the last cycle; entries skipped")
            if blocked_reason is None:
                blocked_reason = "no_new_candle"
                await self.audit.record(
                    "cycle.no_new_candle",
                    "cycle",
                    cycle_id,
                    {
                        "mode": mode.value,
                        "candle_close_at": next(iter(candle_times)).isoformat(),
                    },
                )

        if blocked_reason is None and policy is not None:
            for symbol_policy in policy.universe:
                snapshot = snapshot_map.get(symbol_policy.symbol)
                if snapshot is None:
                    continue
                wait = await self._overtrade_wait(symbol_policy.symbol, started)
                if wait > 0:
                    counters["entries_blocked"] += 1
                    notes.append(
                        f"{symbol_policy.symbol}: over-trading guard, next entry in "
                        f"{int(wait)}s"
                    )
                    await self.audit.record(
                        "signal.overtrade_blocked",
                        "symbol",
                        symbol_policy.symbol,
                        {
                            "mode": mode.value,
                            "window_seconds": self.settings.overtrade_window_seconds,
                            "max_entries": self.settings.overtrade_max_entries,
                            "wait_seconds": int(wait),
                        },
                    )
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

        if self.counterfactual is not None:
            # Reads only: replays blocked buys that have now played out. A
            # failure here is audited inside the service and never blocks
            # the cycle result.
            try:
                replayed = await self.counterfactual.evaluate_pending(
                    datetime.now(UTC),
                    available_quote_usdt=final_account.available_quote_usdt,
                )
                if replayed:
                    notes.append(f"counterfactual outcomes recorded: {replayed}")
            except Exception:  # noqa: BLE001 - data collection only
                notes.append("counterfactual replay failed")

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
            settings=self.settings,
        )
        effective, _row = await self.policy_service.generate(
            generator=self.policy_generator,
            mode=mode.value,
            account_state=account_state,
            market_summary=market_summary,
            purpose=purpose,
            toolbox=toolbox,
            cycle_id=cycle_id,
            budgets=self.budgets,
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

    def _regime_verdict(self, snapshot, policy: EffectivePolicy):
        """Ask the cost-aware regime gate about this snapshot.

        Uses the policy's own holding window: the comparison is between the
        move available over that window and the round trip paid to capture it,
        so a policy that holds longer is rightly judged against a bigger move.
        """

        return self.regime_gate.evaluate(
            features=snapshot.features,
            horizon_seconds=policy.exit_defaults.max_holding_seconds,
        )

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

    def _entry_notional(self, symbol_policy, account, rules) -> Decimal:
        """What one entry may spend, or zero when that is below the exchange minimum."""

        return entry_notional(self.settings, symbol_policy, account, rules)

    def _entry_limit_price(self, snapshot: MarketSnapshot, rules):
        slippage = self.settings.slippage_bps / Decimal("10000")
        price = rules.quantize_price(snapshot.ask * (Decimal("1") + slippage))
        if price < snapshot.ask:
            price += rules.tick_size
        return min(price, rules.max_price)

    async def _jev_pace_wait(self, symbol: str, now: datetime) -> float:
        """Seconds Jev must wait before this symbol may be asked again.

        The spec allows at most one Jev call per closed 1h candle, so a
        symbol is not asked again until the server's minimum interval has
        passed since its last signal reached the market (rejected signals
        included: the call was paid for). Combined with the new-candle check,
        replaying one candle can never spend the day's budget. Server side;
        exits do not consult Jev, so pacing never delays a close.
        """

        interval = int(self.settings.jev_min_call_interval_seconds)
        if interval <= 0:
            return 0.0
        last_reached = await self.repo.last_market_reached_at(
            symbol, JEV_CONSUMED_SIGNAL_STATES
        )
        if last_reached is None:
            return 0.0
        if last_reached.tzinfo is None:
            last_reached = last_reached.replace(tzinfo=UTC)
        elapsed = (now - last_reached).total_seconds()
        return max(0.0, min(interval - elapsed, 86_400.0))

    async def _overtrade_wait(self, symbol: str, now: datetime) -> float:
        """Seconds the over-trading guard holds this symbol back, 0 if free.

        Freqtrade's guards cap how often one pair may trade regardless of the
        strategy; here a sliding window over entry signals does the same. The
        window is a server setting so a policy (or the model writing it) can
        loosen the strategy but never the guard.
        """

        window = int(self.settings.overtrade_window_seconds)
        maximum = int(self.settings.overtrade_max_entries)
        if window <= 0 or maximum <= 0:
            return 0.0
        recent = await self.repo.recent_entry_signal_times(
            symbol,
            now - timedelta(seconds=window),
            MARKET_REACHED_SIGNAL_STATES,
        )
        if len(recent) < maximum:
            return 0.0
        oldest = recent[-maximum]
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        return max(0.0, (oldest + timedelta(seconds=window) - now).total_seconds())

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
        # The same closed candle is never evaluated twice. The in-memory check
        # at cycle start dies with the process, so a restart inside one candle
        # would re-ask Jev (and pay again) about evidence that has not moved.
        already = await self.repo.signal_for_candle(symbol, snapshot.candle_close_at)
        if already is not None:
            counters["entries_blocked"] += 1
            notes.append(f"{symbol}: candle already evaluated, Jev not re-asked")
            await self.audit.record(
                "signal.candle_already_evaluated",
                "symbol",
                symbol,
                {
                    "candle_close_at": snapshot.candle_close_at.isoformat(),
                    "signal_id": already.id,
                },
            )
            return
        # Pace before a signal exists: a symbol Jev may not be asked about
        # again yet has not produced a signal, and creating one only to leave
        # it dangling in a state the machine cannot express is worse than not
        # creating it at all.
        paused = await self._jev_pace_wait(symbol, now)
        if paused > 0:
            counters["entries_blocked"] += 1
            notes.append(f"{symbol}: Jev paced, next call in {int(paused)}s")
            await self.audit.record(
                "signal.paced",
                "symbol",
                symbol,
                {
                    "reason": "jev_candle_pacing",
                    "min_interval_seconds": self.settings.jev_min_call_interval_seconds,
                    "wait_seconds": int(paused),
                },
            )
            return
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
        if self.budgets is not None:
            # The per-call gate: one cheap aggregate read, because checking at
            # cycle start alone would let the last symbols of a cycle overshoot
            # the cap. Kept outside the try below: a refusal means Jev was
            # never called, so it must not be recorded as a paid call (and
            # must not start the cadence wait).
            try:
                await self.budgets.check_jev()
            except AppError as exc:
                await self.repo.transition_signal(
                    signal.id, SignalState.EXPIRED, {"reason": exc.code.value}
                )
                counters["entries_blocked"] += 1
                notes.append(f"{symbol}: Jev call skipped, {exc.message}")
                await self.audit.record(
                    "jev.budget_exhausted",
                    "signal",
                    signal.id,
                    {"symbol": symbol, "code": exc.code.value, **exc.details},
                )
                return
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

        # The regime verdict is computed and recorded for every signal that
        # cleared the Jev gate, in shadow and in enforcement alike, so the
        # cohorts can be compared on outcomes later. It is deliberately placed
        # after scoring: a gate that ran before Jev would leave no outcome to
        # judge it by.
        regime = self._regime_verdict(snapshot, policy)
        gate_detail = {**gate_detail, **regime.as_detail()}
        if regime.blocked and self.settings.regime_gate_enforce:
            await self.repo.transition_signal(
                signal.id, SignalState.REJECTED, gate_detail
            )
            counters["rejected"] += 1
            await self.audit.record(
                "signal.rejected",
                "signal",
                signal.id,
                {"symbol": symbol, "stage": "regime_gate", **gate_detail},
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
        if regime.blocked:
            # Shadow: the trade still happens, but the record says what the
            # gate would have done. This is the only honest way to start with
            # this little data.
            await self.audit.record(
                "regime.shadow_block",
                "signal",
                signal.id,
                {"symbol": symbol, **gate_detail},
            )

        # The edge evaluator asks a different question from either gate: have
        # trades in this cohort actually paid after costs? It is computed for
        # every signal that reaches ELIGIBLE and never blocks one -- there is
        # not enough history for its verdict to earn that right yet. A failure
        # here is audited and the trade proceeds: data collection must not be
        # able to stop trading.
        if self.edge_evaluator is not None:
            try:
                edge = await self.edge_evaluator.evaluate(
                    signal_id=signal.id,
                    symbol=symbol,
                    features=dict(snapshot.features or {}),
                    probability=jev_result.entry_probability,
                    horizon_seconds=policy.exit_defaults.max_holding_seconds,
                    round_trip_cost_bps=self.cost_model.round_trip_bps,
                    now=datetime.now(UTC),
                )
            except Exception as exc:  # noqa: BLE001 - shadow only
                await self.audit.record(
                    "edge.shadow_error",
                    "signal",
                    signal.id,
                    {"symbol": symbol, "error": type(exc).__name__},
                )
            else:
                gate_detail = {**gate_detail, **edge.as_detail()}
                if edge.blocked:
                    await self.audit.record(
                        "edge.shadow_block",
                        "signal",
                        signal.id,
                        {"symbol": symbol, **edge.as_detail()},
                    )

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

        # One order per symbol per closed candle, whatever produced the signal.
        # The key below includes the policy, so two policies on the same candle
        # would otherwise both be allowed to buy.
        existing_intent = await self.repo.intent_for_candle(
            symbol, snapshot.candle_close_at
        )
        if existing_intent is not None:
            await self.repo.transition_signal(
                signal_id,
                SignalState.EXPIRED,
                {"reason": "candle_already_traded", "intent_id": existing_intent.id},
            )
            notes.append(f"{symbol}: candle already has an intent")
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

        quote_notional = self._entry_notional(symbol_policy, account, rules)
        if quote_notional <= ZERO:
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
            exit_stress_drawdown_bps=self.settings.exit_stress_drawdown_bps,
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

        # The risk decision was made against an earlier snapshot. Price, spread
        # and account state move between approval and dispatch, so the decision
        # is re-derived here against what the order would actually face; a
        # decision that no longer holds blocks the order instead of filling it
        # at a price the risk engine never approved.
        try:
            await self._recheck_risk(approved, snapshot, rules, now)
        except AppError as exc:
            await self.repo.transition_signal(
                row.signal_id,
                SignalState.ERROR,
                {"reason": exc.code.value, "stage": "dispatch_recheck"},
            )
            await self.repo.update_intent_status(intent_id, "ERROR")
            await self.audit.record(
                "execution.recheck_failed",
                "intent",
                intent_id,
                {"code": exc.code.value, **exc.details},
            )
            return {
                "intent_id": intent_id,
                "status": SignalState.ERROR.value,
                "order_id": None,
                "notes": [f"risk re-check failed: {exc.message}"],
            }

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

    async def _recheck_risk(self, approved, snapshot, rules, now: datetime) -> None:
        """Re-evaluate the approval against the snapshot the order will use.

        Only the checks that a moving market can invalidate are re-run: the
        intent's own numbers, its policy and its TTL were already settled. A
        stale or widened book, a halted symbol, or a spread the policy no
        longer tolerates must stop the order, not be inherited from the
        earlier decision.
        """

        intent = approved.intent
        policy_row = await self.repo.get_policy_by_policy_id(str(intent.policy_id))
        if policy_row is None:
            raise AppError(
                ErrorCode.POLICY_EXPIRED,
                "The policy behind this intent is no longer stored.",
            )
        policy = EffectivePolicy.model_validate(policy_row.effective_json)
        if policy.expires_at <= now:
            raise AppError(
                ErrorCode.POLICY_EXPIRED,
                f"The policy expired at {policy.expires_at.isoformat()}.",
            )
        symbol_policy = policy.symbol_policy(intent.symbol)
        if symbol_policy is None:
            raise AppError(
                ErrorCode.POLICY_EXPIRED,
                f"The policy no longer covers {intent.symbol}.",
            )
        marks = {intent.symbol: snapshot.mid}
        account = await self.repo.account_snapshot(self.account_id, marks, now)
        positions = await self.repo.get_open_positions(self.account_id)
        held = next(
            (position.qty for position in positions if position.symbol == intent.symbol),
            ZERO,
        )
        system = await self._exit_system_snapshot()
        context = RiskContext(
            policy=policy,
            account=account,
            market=snapshot,
            symbol_rules=rules,
            system=system,
            now=now,
            position_qty=held,
            last_entry_at=await self.repo.last_entry_at(self.account_id, intent.symbol),
            max_staleness_ms=self.settings.market_staleness_ms,
            exit_slippage_bps=self.settings.exit_slippage_bps,
            exit_stress_drawdown_bps=self.settings.exit_stress_drawdown_bps,
        )
        # The intent was stamped against the approval-time snapshot, so the
        # hash has to be re-derived for this context; otherwise every re-check
        # would fail on the hash alone rather than on the state that moved.
        provisional = intent.model_copy(update={"risk_snapshot_hash": "pending"})
        checked = provisional.model_copy(
            update={"risk_snapshot_hash": compute_risk_snapshot_hash(provisional, context)}
        )
        decision = self.risk_engine.evaluate(checked, context)
        await self.repo.insert_risk_decision(decision)
        await self.audit.record(
            "risk.recheck",
            "intent",
            str(intent.intent_id),
            {
                "approved": decision.approved,
                "failed_rules": [
                    failure.model_dump(mode="json") for failure in decision.failed_rules
                ],
                "snapshot_id": str(snapshot.snapshot_id),
            },
        )
        if not decision.approved:
            raise RiskRejectedError(
                "The approved intent no longer passes risk at dispatch time.",
                [failure.model_dump(mode="json") for failure in decision.failed_rules],
            )

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
        self,
        position,
        signal,
        adapter,
        now: datetime,
        snapshot: MarketSnapshot,
        notes: list[str],
    ) -> int:
        """Finish or back out a close order that is still EXIT_PENDING.

        The exit manager used to skip EXIT_PENDING signals outright, so a
        resting close (or any non-AppError) left the position permanently
        unmanaged. This reconciles the working close with its recorded order:

        * filled        -> CLOSED
        * rejected/error -> POSITION_OPEN (a fresh exit may be attempted)
        * stale          -> cancelled after RESTING_CLOSE_REPRICE_SECONDS (the
                            bid has moved away; re-price near the current one)
        * still working  -> cancelled once older than the working limit, then
                            POSITION_OPEN once the cancel is acknowledged

        Returns the number of exits completed, and -1 when a stale close was
        cancelled so the caller can re-price this cycle instead of next.
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
        # How long the order has been working, measured the same two ways the
        # two clocks allow: wall time since creation, or how many closed
        # candles have printed since. This order was placed on the cycle where
        # "now" was at or after its creation, so the wall clock can be used.
        age = (now - created).total_seconds()
        if age < CLOSE_ORDER_WORKING_LIMIT_SECONDS:
            stale = (
                age >= RESTING_CLOSE_REPRICE_SECONDS
                and _needs_reprice(order.price, snapshot.bid)
            )
            if not stale:
                notes.append(f"{position.symbol}: exit order working")
                return 0
            repriced = await self._cancel_and_reprice(
                position, signal, adapter, order, intent, now, age, snapshot, notes
            )
            return -1 if repriced else 0
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

    async def _cancel_and_reprice(
        self,
        position,
        signal,
        adapter,
        order,
        intent,
        now: datetime,
        age: float,
        snapshot: MarketSnapshot,
        notes: list[str],
    ) -> bool:
        """Cancel a resting close whose price the market has left behind."""

        state_note = {
            "symbol": position.symbol,
            "order_id": order.id,
            "order_limit": canonical_str(order.price) if order.price else None,
            "bid": canonical_str(snapshot.bid),
            "age_seconds": int(age),
        }
        try:
            await adapter.cancel(order.id)
        except ReconciliationRequiredError:
            await self.repo.transition_signal(
                signal.id,
                SignalState.RECONCILIATION_REQUIRED,
                {**state_note, "reason": "reprice_cancel_outcome_unknown"},
            )
            if intent is not None:
                await self.repo.update_intent_status(
                    intent.id, "RECONCILIATION_REQUIRED"
                )
            notes.append(f"{position.symbol}: reprice cancel outcome unknown; reconcile")
            return False
        except AppError as exc:
            notes.append(f"{position.symbol}: reprice cancel failed ({exc.code.value})")
            return False
        # Cheap placeholders (paper, shadow) cancel locally without touching the
        # order row. Re-read it so a close the cancel did not actually stop
        # (it filled meanwhile) is not re-priced on top of a filled position.
        current = await self.repo.get_order(order.id)
        if current is not None and current.status == OrderStatus.FILLED.value:
            await self.repo.transition_signal(
                signal.id, SignalState.CLOSED, {**state_note, "source": "filled_during_reprice"}
            )
            if intent is not None:
                await self.repo.update_intent_status(intent.id, "EXECUTED")
            notes.append(f"{position.symbol}: exit filled while being re-priced")
            return False
        await self.repo.transition_signal(
            signal.id,
            SignalState.POSITION_OPEN,
            {**state_note, "reason": "close_repriced"},
        )
        if intent is not None:
            await self.repo.update_intent_status(intent.id, "CANCELLED")
        await self.audit.record("exit.repriced", "position", position.id, state_note)
        await self.broker.publish(
            "execution.events",
            {"event": "exit_reprice", "symbol": position.symbol, **state_note},
        )
        self._reprice_guard[position.id] = _snapshot_key(snapshot)
        notes.append(f"{position.symbol}: resting exit re-priced to the current bid")
        return True

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
        return await self._route_exits(
            policy=policy, snapshot_map=snapshot_map, system=system
        )

    async def _route_exits(
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
        self._repriced_positions.clear()
        positions = await self.repo.get_open_positions(self.account_id)
        for position in positions:
            repriced_this_cycle = False
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
                # A close already chased against this very snapshot must wait
                # for the next one: re-pricing the same bid would only churn.
                if self._reprice_guard.get(position.id) == _snapshot_key(snapshot):
                    notes.append(
                        f"{position.symbol}: exit re-pricing deferred to the next candle"
                    )
                    continue
                # A cancelled re-price leaves the original stop reason in
                # place, so the fresh close below is generated at once.
                resolved = await self._resolve_pending_close(
                    position, signal, adapter, now, snapshot, notes
                )
                # A negative value is not an exit count: it says the resting
                # close was cancelled and this cycle should re-price it.
                exits += max(resolved, 0)
                if resolved < 0:
                    # Re-read through a fresh identity: the re-price wrote to
                    # the same row this object was loaded from.
                    position = await self.repo.settled_position(position.id)
                    if position.status != PositionStatus.OPEN.value:
                        continue
                    signal = await self.repo.get_signal(position.signal_id)
                    if signal is None or SignalState(signal.state) is not SignalState.POSITION_OPEN:
                        continue
                    adapter = self.execution_for_mode.get(self._mode_of(position)) or adapter
                    execute = self._mode_of(position) is not await self.modes.current_mode()
                    state = SignalState(signal.state)
                    repriced_this_cycle = True
                    self._repriced_positions.add(position.id)
                else:
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
            if repriced_this_cycle or position.id in self._repriced_positions:
                # The cancelled order may share this minute's key (even if the
                # re-price happened in an earlier cycle); the replacement
                # needs its own so the insert is not skipped as a duplicate.
                # The chased candle identifies it: the guard allows at most one
                # re-price per candle, so the key stays unique and stable.
                idempotency_suffix = f"reprice-{_snapshot_key(snapshot)[1]}"
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

            if position.exit_requested_reason and reason == position.exit_requested_reason:
                # The operator asked for this close: fill it as a marketable
                # order so it executes now, and anchor fills on the bid (never
                # the ask) so realized PnL does not absorb the spread.
                order_preference = OrderPreference.MARKET
                limit_price = None
            else:
                # Binance spot rejects any SELL whose limit is at or above the
                # bid ("would immediately trigger"). Keeping the floor just
                # below the bid at submit time makes the order cross whenever
                # it still can, and rest only if the bid fell meanwhile.
                order_preference = OrderPreference.LIMIT
                limit_price = _marketable_sell_price(snapshot.bid, rules)

            quote_notional = max(position.qty * snapshot.bid, rules.min_notional)
            if limit_price is not None and limit_price <= ZERO:
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
                order_preference=order_preference,
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
                exit_stress_drawdown_bps=self.settings.exit_stress_drawdown_bps,
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
                if order_preference is OrderPreference.MARKET:
                    notes.append(
                        f"{position.symbol}: manual close resting at the bid; re-priced next cycle"
                    )
                else:
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
