"""Counterfactual outcomes for buy signals the Jev gate blocked.

A rejected signal leaves no ledger row, so the only outcomes the system can
observe are the ones the gate admitted. That makes "is the gate too strict?"
unanswerable from the record: a blocked idea that would have paid is
indistinguishable from one that would have stopped out.

This service replays each blocked buy against the candles that followed, under
the exit rules its policy actually carried, and stores the result. It never
places an order and never touches the trading path.

The replay is deliberately pessimistic, so a finding of "the gate cost us
money" cannot come from optimistic assumptions:

- entry pays the ask plus entry slippage, at the snapshot captured for the
  signal;
- only bars after the signal minute are examined, so the bar in which the
  entry would have happened can never be used to reach the target;
- when a single 1m bar touches both the stop and the target, the stop wins;
- every exit pays exit slippage and both sides pay the taker fee.

Known imprecision: if the entry happens part-way through a 1m bar, a stop or
target touched later in that same minute is only seen from the next bar on.
Nothing cheaper than trade-level data can resolve that. Two things make the
gap visible instead of hidden: ``gap_seconds`` counts the minutes of the
window that no bar covered, and a window with no bars at all is stored as
``no_data`` rather than as a flat timeout.

Coverage is bounded: only signals inside ``LOOKBACK`` are replayed, because
the venue serves at most 1000 one-minute bars per request. A signal older
than that is not recorded at all -- it is not retried forever.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from ..domain.market import Candle
from ..domain.money import ZERO
from ..domain.policy import EffectivePolicy, ExitDefaults
from .klines import fetch_minute_range

SIMULATION_METHOD = "1m-conservative-v1"
#: Binance returns at most 1000 one-minute bars, so a signal older than this
#: can no longer be replayed faithfully.
LOOKBACK = timedelta(hours=14)
#: Signals replayed per cycle. Each one costs a klines walk; a backlog is
#: picked up by the next cycle, so this only bounds the work in one cycle.
MAX_REPLAYS_PER_CYCLE = 6


@dataclass(frozen=True)
class Replay:
    entry_price: Decimal
    exit_price: Decimal
    exit_reason: str
    net_bps: Decimal
    pnl_usdt: Decimal
    exit_time: datetime | None = None
    mfe_bps: Decimal = ZERO
    mae_bps: Decimal = ZERO
    minutes_judged: int = 0
    gap_seconds: int = 0


def simulate(
    *,
    candles: Sequence[Candle],
    entry_price: Decimal,
    exit_rules: ExitDefaults,
    horizon_end: datetime,
    taker_fee_bps: Decimal,
    exit_slippage_bps: Decimal,
    notional: Decimal,
) -> Replay | None:
    """Replay one long entry over ``candles``; ``None`` when they prove nothing.

    ``candles`` must already be restricted to the bars that follow the entry
    minute. The result is expressed net of fees and exit slippage.
    """

    if not candles or entry_price <= ZERO:
        return None
    slippage = exit_slippage_bps / Decimal("10000")
    stop_price = entry_price * (Decimal("1") - exit_rules.stop_loss_bps / Decimal("10000"))
    target_price = entry_price * (Decimal("1") + exit_rules.take_profit_bps / Decimal("10000"))

    exit_price: Decimal | None = None
    exit_reason = "timeout"
    exit_time: datetime | None = None
    last_close: Decimal | None = None
    highest = entry_price
    lowest = entry_price
    minutes_judged = 0
    for candle in candles:
        if candle.close_time > horizon_end:
            break
        minutes_judged += 1
        last_close = Decimal(str(candle.close))
        low = Decimal(str(candle.low))
        high = Decimal(str(candle.high))
        highest = max(highest, high, last_close)
        lowest = min(lowest, low, last_close)
        if low <= stop_price:
            # One bar can touch both levels; assuming the stop removes any
            # benefit of hindsight from intra-bar ordering we cannot see.
            exit_price = stop_price * (Decimal("1") - slippage)
            exit_reason = "stop_loss"
            exit_time = candle.close_time
            break
        if high >= target_price:
            exit_price = target_price * (Decimal("1") - slippage)
            exit_reason = "take_profit"
            exit_time = candle.close_time
            break
    if exit_price is None:
        if last_close is None:
            return None
        exit_price = last_close * (Decimal("1") - slippage)
        exit_time = candles[-1].close_time

    gross_bps = (exit_price / entry_price - Decimal("1")) * Decimal("10000")
    net_bps = gross_bps - taker_fee_bps * Decimal("2")
    pnl = notional * net_bps / Decimal("10000")
    return Replay(
        entry_price=entry_price,
        exit_price=exit_price,
        exit_reason=exit_reason,
        net_bps=net_bps,
        pnl_usdt=pnl,
        exit_time=exit_time,
        mfe_bps=(highest / entry_price - Decimal("1")) * Decimal("10000"),
        mae_bps=(lowest / entry_price - Decimal("1")) * Decimal("10000"),
        minutes_judged=minutes_judged,
    )


def blocked_rule(policy: EffectivePolicy, score: dict[str, Any]) -> str:
    """Which leg of the gate held this buy signal shut."""

    gate = policy.jev_gate
    if str(score.get("entry_choice")) != gate.entry_choice.value:
        return "entry_choice"
    if float(score.get("entry_probability") or 0.0) < gate.min_choice_probability:
        return "min_choice_probability"
    if float(score.get("policy_fit") or 0.0) < gate.min_policy_fit:
        return "min_policy_fit"
    if float(score.get("quality_score") or 0.0) < gate.min_quality_score:
        return "min_quality_score"
    return "unknown"


class CounterfactualService:
    """Replays blocked buy signals once their holding window has elapsed."""

    def __init__(self, *, settings, repo, market, audit: Any | None = None) -> None:
        self.settings = settings
        self.repo = repo
        self.market = market
        self.audit = audit

    async def evaluate_pending(
        self,
        now: datetime | None = None,
        *,
        available_quote_usdt: Decimal | None = None,
    ) -> int:
        """Write any outcomes that are now fully determined. Returns the count.

        Per-signal failures are swallowed and retried next cycle: a replay that
        cannot fetch candles must never interrupt trading or lose the record
        permanently.
        """

        now = now or datetime.now(UTC)
        candidates = await self.repo.list_rejected_signals_without_counterfactual(
            since=now - LOOKBACK, until=now, limit=MAX_REPLAYS_PER_CYCLE
        )
        written = 0
        for signal in candidates:
            try:
                row = await self._evaluate(signal, now, available_quote_usdt)
            except Exception as exc:  # noqa: BLE001 - replay is best-effort
                if self.audit is not None:
                    await self.audit.record(
                        "counterfactual.error",
                        "signal",
                        signal.id,
                        {"symbol": signal.symbol, "error": type(exc).__name__},
                    )
                continue
            if row is not None:
                written += 1
        return written

    async def _evaluate(
        self, signal: Any, now: datetime, available_quote_usdt: Decimal | None
    ) -> Any | None:
        score = dict(signal.score_json or {})
        if str(score.get("entry_choice")) != "buy":
            return None

        policy_row = await self.repo.get_policy_by_policy_id(signal.policy_id)
        if policy_row is None:
            return None
        policy = EffectivePolicy.model_validate(policy_row.effective_json)
        symbol_policy = policy.symbol_policy(signal.symbol)
        if symbol_policy is None:
            return None

        horizon_end = signal.created_at + timedelta(
            seconds=policy.exit_defaults.max_holding_seconds
        )
        if horizon_end > now:
            # The replay would still be guessing at the tail of the window.
            return None

        snapshot = await self.repo.get_market_snapshot(signal.snapshot_id)
        if snapshot is None:
            return None
        rules = await self.market.filters(signal.symbol)

        requested = min(
            symbol_policy.max_position_notional_usdt,
            self.settings.max_position_notional_usdt,
        )
        if available_quote_usdt is not None:
            requested = min(
                requested,
                available_quote_usdt * self.settings.entry_budget_fraction,
            )
        notional = requested if requested >= rules.min_notional else ZERO

        entry_price = snapshot.ask * (Decimal("1") + self.settings.slippage_bps / Decimal("10000"))
        entry_price = rules.quantize_price(entry_price)

        # Ask for the window itself: the provider returns the *latest* 1000
        # bars when no start is given, which for an older signal is no bars at
        # all. Bars that opened before the signal are dropped, so the entry
        # minute can never carry the exit.
        candles = await fetch_minute_range(
            self.market, signal.symbol, signal.created_at, horizon_end
        )
        window = [candle for candle in candles if candle.open_time > signal.created_at]
        replay = simulate(
            candles=window,
            entry_price=entry_price,
            exit_rules=policy.exit_defaults,
            horizon_end=horizon_end,
            taker_fee_bps=rules.taker_fee_bps,
            exit_slippage_bps=self.settings.exit_slippage_bps,
            notional=notional,
        )
        if replay is None:
            # No bars in the window at all. Stored, not skipped: a signal the
            # venue has no history for must not be scanned every cycle.
            return await self.repo.insert_counterfactual(
                signal_id=signal.id,
                policy_id=signal.policy_id,
                symbol=signal.symbol,
                decided_at=signal.created_at,
                entry_choice=str(score.get("entry_choice")),
                entry_probability=float(score.get("entry_probability") or 0.0),
                policy_fit=float(score.get("policy_fit") or 0.0),
                quality_score=float(score.get("quality_score") or 0.0),
                blocked_rule=blocked_rule(policy, score),
                assumed_notional_usdt=notional,
                entry_price=entry_price,
                exit_price=None,
                exit_reason="no_data",
                net_bps=None,
                pnl_usdt=None,
                horizon_seconds=policy.exit_defaults.max_holding_seconds,
                mfe_bps=None,
                mae_bps=None,
                holding_seconds=None,
                gap_seconds=policy.exit_defaults.max_holding_seconds,
                is_synthetic=bool(snapshot.is_synthetic),
                method=SIMULATION_METHOD,
            )

        return await self.repo.insert_counterfactual(
            signal_id=signal.id,
            policy_id=signal.policy_id,
            symbol=signal.symbol,
            decided_at=signal.created_at,
            entry_choice=str(score.get("entry_choice")),
            entry_probability=float(score.get("entry_probability") or 0.0),
            policy_fit=float(score.get("policy_fit") or 0.0),
            quality_score=float(score.get("quality_score") or 0.0),
            blocked_rule=blocked_rule(policy, score),
            assumed_notional_usdt=notional,
            entry_price=replay.entry_price,
            exit_price=replay.exit_price,
            exit_reason=replay.exit_reason,
            net_bps=replay.net_bps,
            pnl_usdt=replay.pnl_usdt if notional > ZERO else None,
            horizon_seconds=policy.exit_defaults.max_holding_seconds,
            mfe_bps=replay.mfe_bps,
            mae_bps=replay.mae_bps,
            holding_seconds=int(
                max(
                    0.0,
                    (replay.exit_time - signal.created_at).total_seconds(),
                )
            )
            if replay.exit_time is not None
            else None,
            # Minutes the window covers that no 1m bar was judged for: a
            # data gap, not a quiet market.
            gap_seconds=60
            * max(0, policy.exit_defaults.max_holding_seconds // 60 - replay.minutes_judged),
            is_synthetic=bool(snapshot.is_synthetic),
            method=SIMULATION_METHOD,
        )
