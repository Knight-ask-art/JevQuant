"""The exit rule, in one place, so live and replay cannot disagree.

A position has three ways to leave: a stop, a target, or the clock. Version 2
adds two ways for the stop to ratchet -- break-even and trailing -- and both are
*off unless the policy asks for them*, because a default that tightens the stop
is a silent change to every trade that was already being made.

The rule is written as a pure function over prices because it is used twice:

* by the orchestrator, once per cycle, against the current bid;
* by the counterfactual replay, once per 1m bar, against that bar's low/high.

If those two ever diverge, the recorded "what would this have returned" stops
describing the rule that actually runs. Sharing the function is the cheapest
way to make that impossible.

**What the peak is, and is not.** The ratchet needs a high-water mark, and the
system only observes prices when it looks. The orchestrator updates the peak
from the bid it sees each cycle, so a spike between cycles is invisible and the
trail arms later than it would with tick data. That is the safe direction: a
late trail holds the position longer, it does not exit it early. The replay
sees every bar's high, so replay results are slightly more optimistic about the
ratchet than live -- documented rather than papered over.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
BPS = Decimal("10000")


@dataclass(frozen=True)
class ExitGeometry:
    """The exit rules frozen onto a position when it opened."""

    stop_loss_bps: Decimal = ZERO
    take_profit_bps: Decimal = ZERO
    max_holding_seconds: int = 0
    #: Once the peak is this far above entry, the stop moves to entry + cost.
    breakeven_trigger_bps: Decimal | None = None
    #: Once the peak is this far above entry, the stop trails the peak.
    trailing_trigger_bps: Decimal | None = None
    #: How far below the peak the trailing stop sits.
    trailing_distance_bps: Decimal | None = None


@dataclass(frozen=True)
class ExitPlan:
    """Where the stop and target sit right now, and whether one was crossed."""

    stop_price: Decimal
    target_price: Decimal
    #: The stop after any ratchet; equal to the original when nothing armed.
    reason: str | None
    ratcheted: bool

    def as_detail(self) -> dict[str, str | bool | None]:
        return {
            "stop_price": str(self.stop_price),
            "target_price": str(self.target_price),
            "ratcheted": self.ratcheted,
        }


def initial_stop_price(entry_price: Decimal, stop_loss_bps: Decimal) -> Decimal:
    if stop_loss_bps <= ZERO:
        return ZERO
    return entry_price * (Decimal("1") - stop_loss_bps / BPS)


def exit_plan(
    *,
    entry_price: Decimal,
    geometry: ExitGeometry,
    peak_price: Decimal | None,
    bid: Decimal,
    round_trip_cost_bps: Decimal = ZERO,
) -> ExitPlan:
    """Price the stop and target, then say whether the bid crossed either.

    ``peak_price`` is the high-water mark of observed prices since entry; a
    ``None`` or non-positive value means "nothing observed yet" and disables
    the ratchets for this call. ``round_trip_cost_bps`` is what the position
    must recover to break even, so a break-even stop sits above entry by that
    much rather than exactly at it -- a stop at entry still loses the fees.
    """

    stop_price = initial_stop_price(entry_price, geometry.stop_loss_bps)
    target_price = (
        entry_price * (Decimal("1") + geometry.take_profit_bps / BPS)
        if geometry.take_profit_bps > ZERO
        else ZERO
    )

    peak = peak_price if peak_price is not None and peak_price > ZERO else None
    ratcheted = False
    if peak is not None and peak > entry_price:
        gain_bps = (peak / entry_price - Decimal("1")) * BPS
        if (
            geometry.breakeven_trigger_bps is not None
            and gain_bps >= geometry.breakeven_trigger_bps
        ):
            break_even = entry_price * (Decimal("1") + round_trip_cost_bps / BPS)
            if break_even > stop_price:
                stop_price = break_even
                ratcheted = True
        if (
            geometry.trailing_trigger_bps is not None
            and geometry.trailing_distance_bps is not None
            and gain_bps >= geometry.trailing_trigger_bps
        ):
            trailed = peak * (Decimal("1") - geometry.trailing_distance_bps / BPS)
            if trailed > stop_price:
                stop_price = trailed
                ratcheted = True

    reason: str | None = None
    # The stop is checked first: a bar that touches both levels is a stop, in
    # the replay and here alike, because intra-bar order is not observable.
    if stop_price > ZERO and bid <= stop_price:
        reason = "stop_loss"
    elif target_price > ZERO and bid >= target_price:
        reason = "take_profit"
    return ExitPlan(
        stop_price=stop_price,
        target_price=target_price,
        reason=reason,
        ratcheted=ratcheted,
    )


def time_limit_reached(
    *, opened_at, now, max_holding_seconds: int
) -> bool:
    """The clock only. Kept separate so the caller's timezone handling stays visible."""

    from datetime import timedelta

    if not max_holding_seconds:
        return False
    return now >= opened_at + timedelta(seconds=max_holding_seconds)
