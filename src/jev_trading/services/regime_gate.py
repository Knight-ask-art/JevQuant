"""A deterministic "is this market worth the cost of trading?" gate.

The system can already answer "should I buy this candidate". It could not
answer "should I be trading at all", and the recorded data says that is the
more valuable question: every group in the three-day replay lost money while
holding cash returned zero.

This gate is not a model and does not look at Jev's answer. It compares what
the market can plausibly deliver over the holding window against what a round
trip costs, and refuses the trade when the first cannot cover the second:

    expected absolute move over the horizon  vs  round-trip cost x multiplier

Both sides are estimates, so the result is recorded on every signal whether or
not it blocks. That matters for two reasons:

- the verdict is queryable per signal, so blocked and allowed cohorts can be
  compared on realised outcomes instead of argued about;
- it can run in shadow, recording what it *would* have done while the trade
  still happens. With this little data, that is the only honest way to start.

The gate never places, sizes, or approves anything. It can only refuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import sqrt
from typing import Any

from ..domain.costs import CostModel

ZERO = Decimal("0")
BPS = Decimal("10000")

#: Assumed volatility reference window, in minutes. ``atr_5m_bps`` is the mean
#: true range of a 5-minute bar, so it scales from there.
ATR_WINDOW_MINUTES = 5

# Mirrors the Settings default, for callers that pass a partial settings object
# (tests build a stub with only the fields their path touches). The fee and
# slippage sides come from ``CostModel`` so the gate compares against the same
# round trip the execution path and the replay charge.
DEFAULT_COST_MULTIPLIER = Decimal("1.5")


@dataclass(frozen=True)
class RegimeDecision:
    """One verdict about one snapshot. ``blocked`` is what the gate advises."""

    blocked: bool
    reason: str
    expected_abs_move_bps: Decimal | None
    required_move_bps: Decimal | None
    round_trip_cost_bps: Decimal
    horizon_minutes: int
    atr_bps: Decimal | None

    @property
    def coverage(self) -> Decimal | None:
        """Expected move as a multiple of the required move; >= 1 clears the gate."""

        if self.expected_abs_move_bps is None or self.required_move_bps in (None, ZERO):
            return None
        return self.expected_abs_move_bps / self.required_move_bps

    def as_detail(self) -> dict[str, Any]:
        return {
            "regime_blocked": self.blocked,
            "regime_reason": self.reason,
            "regime_expected_abs_move_bps": _round(self.expected_abs_move_bps),
            "regime_required_move_bps": _round(self.required_move_bps),
            "regime_round_trip_cost_bps": _round(self.round_trip_cost_bps),
            "regime_horizon_minutes": self.horizon_minutes,
            "regime_atr_bps": _round(self.atr_bps),
            "regime_coverage": _round(self.coverage),
        }


def _round(value: Decimal | None) -> float | None:
    return None if value is None else round(float(value), 2)


def round_trip_cost_bps(
    *, fee_bps: Decimal, entry_slippage_bps: Decimal, exit_slippage_bps: Decimal
) -> Decimal:
    """Both sides' fees plus the slippage paid on entry and on exit."""

    return fee_bps * Decimal("2") + entry_slippage_bps + exit_slippage_bps


def expected_abs_move_bps(
    atr_bps: Decimal, horizon_minutes: int, *, reference_minutes: int = ATR_WINDOW_MINUTES
) -> Decimal:
    """Scale a 5-minute mean true range to the holding horizon.

    A random walk's mean absolute displacement grows with the square root of
    time, so this is the estimate under that (deliberately simple) assumption.
    It is an *absolute* move: the trade still has to get the direction right,
    which makes the comparison against cost conservative in the useful way --
    it overstates how often the horizon pays.
    """

    if horizon_minutes <= 0 or reference_minutes <= 0:
        return ZERO
    return atr_bps * Decimal(str(sqrt(horizon_minutes / reference_minutes)))


class RegimeGate:
    """Cost-aware market filter. Deterministic, read-only, refusing-only."""

    def __init__(
        self,
        *,
        cost_multiplier: Decimal,
        fee_bps: Decimal,
        entry_slippage_bps: Decimal,
        exit_slippage_bps: Decimal,
    ) -> None:
        self.cost_multiplier = cost_multiplier
        self.fee_bps = fee_bps
        self.entry_slippage_bps = entry_slippage_bps
        self.exit_slippage_bps = exit_slippage_bps

    @classmethod
    def from_settings(cls, settings: Any) -> RegimeGate:
        """Build from a Settings object, tolerating a partial one.

        Tests construct a settings stub with only the fields the code path they
        exercise needs; ``CostModel`` supplies the same defaults the full
        settings carry.
        """

        cost = CostModel.from_settings(settings)
        return cls(
            cost_multiplier=getattr(
                settings, "regime_cost_multiplier", DEFAULT_COST_MULTIPLIER
            ),
            fee_bps=cost.fee_bps,
            entry_slippage_bps=cost.entry_slippage_bps,
            exit_slippage_bps=cost.exit_slippage_bps,
        )

    def evaluate(self, *, features: dict[str, float], horizon_seconds: int) -> RegimeDecision:
        cost = round_trip_cost_bps(
            fee_bps=self.fee_bps,
            entry_slippage_bps=self.entry_slippage_bps,
            exit_slippage_bps=self.exit_slippage_bps,
        )
        required = cost * self.cost_multiplier
        horizon_minutes = max(0, horizon_seconds // 60)

        raw_atr = features.get("atr_5m_bps")
        atr_bps = Decimal(str(raw_atr)) if raw_atr is not None else None
        if atr_bps is None or atr_bps <= ZERO:
            # No volatility estimate means the question cannot be answered.
            # Zero is included on purpose: the feature is 0.0 when the ATR
            # could not be computed (too few 5m bars, or no price), and a real
            # 5-minute true range is never exactly zero. Calling that "a quiet
            # market that cannot pay for itself" would hide a data gap inside
            # a plausible-looking verdict. Fail closed, like every other
            # unknown in the entry path; the shadow switch is what keeps this
            # from stopping trading today.
            return RegimeDecision(
                blocked=True,
                reason="no_volatility_estimate",
                expected_abs_move_bps=None,
                required_move_bps=required,
                round_trip_cost_bps=cost,
                horizon_minutes=horizon_minutes,
                atr_bps=None,
            )

        expected = expected_abs_move_bps(atr_bps, horizon_minutes)
        blocked = expected < required
        return RegimeDecision(
            blocked=blocked,
            reason="expected_move_below_cost" if blocked else "expected_move_covers_cost",
            expected_abs_move_bps=expected,
            required_move_bps=required,
            round_trip_cost_bps=cost,
            horizon_minutes=horizon_minutes,
            atr_bps=atr_bps,
        )


def describe(decision: RegimeDecision) -> str:
    """One line for an audit entry or a cycle note."""

    if decision.expected_abs_move_bps is None:
        return (
            f"{decision.reason}: no ATR, needs {decision.required_move_bps} bps "
            f"over {decision.horizon_minutes} min"
        )
    return (
        f"{decision.reason}: expected {decision.expected_abs_move_bps:.1f} bps "
        f"vs required {decision.required_move_bps:.1f} bps "
        f"over {decision.horizon_minutes} min"
    )
