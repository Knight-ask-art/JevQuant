"""The regime gate: refuse a trade the market cannot pay for."""

from __future__ import annotations

from decimal import Decimal

from jev_trading.services.regime_gate import (
    RegimeGate,
    expected_abs_move_bps,
    round_trip_cost_bps,
)


def gate(multiplier: str = "1.5") -> RegimeGate:
    return RegimeGate(
        cost_multiplier=Decimal(multiplier),
        fee_bps=Decimal("10"),
        entry_slippage_bps=Decimal("2"),
        exit_slippage_bps=Decimal("8"),
    )


def test_round_trip_cost_is_both_fees_plus_both_slippages():
    assert round_trip_cost_bps(
        fee_bps=Decimal("10"),
        entry_slippage_bps=Decimal("2"),
        exit_slippage_bps=Decimal("8"),
    ) == Decimal("30")


def test_expected_move_scales_with_the_square_root_of_the_horizon():
    """A random walk's mean absolute displacement grows as sqrt(time)."""

    one_bar = expected_abs_move_bps(Decimal("10"), 5)
    four_bars = expected_abs_move_bps(Decimal("10"), 20)

    assert one_bar == Decimal("10")
    assert abs(four_bars - Decimal("20")) < Decimal("0.0001")


def test_a_quiet_market_that_cannot_cover_the_round_trip_is_blocked():
    """10 bps ATR over a 90-minute hold cannot pay a 30 bps round trip."""

    decision = gate().evaluate(
        features={"atr_5m_bps": 10.0}, horizon_seconds=90 * 60
    )

    assert decision.blocked is True
    assert decision.reason == "expected_move_below_cost"
    # sqrt(90/5) = sqrt(18) ~ 4.24 -> ~42 bps expected against 45 required.
    assert decision.expected_abs_move_bps < decision.required_move_bps
    assert decision.coverage < Decimal("1")


def test_a_volatile_market_that_can_cover_the_round_trip_is_allowed():
    decision = gate().evaluate(
        features={"atr_5m_bps": 40.0}, horizon_seconds=90 * 60
    )

    assert decision.blocked is False
    assert decision.reason == "expected_move_covers_cost"
    assert decision.coverage > Decimal("1")


def test_the_multiplier_is_what_makes_a_borderline_market_a_refusal():
    """The same market passes at 1.0x and fails at 2.0x: the margin is the knob."""

    # ATR 12 bps over 60 min -> 12 * sqrt(12) ~ 41.6 bps expected: above 30,
    # below 60.
    features = {"atr_5m_bps": 12.0}
    horizon = 60 * 60

    loose = gate("1.0").evaluate(features=features, horizon_seconds=horizon)
    strict = gate("2.0").evaluate(features=features, horizon_seconds=horizon)

    assert loose.blocked is False
    assert strict.blocked is True
    assert loose.required_move_bps == Decimal("30.0")
    assert strict.required_move_bps == Decimal("60.0")


def test_a_missing_volatility_estimate_fails_closed():
    """Unknown is not "safe to trade": it is an unanswered question."""

    decision = gate().evaluate(features={}, horizon_seconds=3600)

    assert decision.blocked is True
    assert decision.reason == "no_volatility_estimate"
    assert decision.expected_abs_move_bps is None
    assert decision.coverage is None


def test_a_zero_atr_is_a_data_gap_not_a_quiet_market():
    """The feature is 0.0 when the ATR could not be computed; say so.

    Both readings block, so enforcement is unaffected -- but "the market was
    too quiet to pay" and "we did not have enough candles to know" are
    different facts, and only the second is a reason to distrust the shadow
    cohort.
    """

    zero = gate().evaluate(features={"atr_5m_bps": 0.0}, horizon_seconds=3600)
    missing = gate().evaluate(features={}, horizon_seconds=3600)

    assert zero.blocked is True
    assert zero.reason == "no_volatility_estimate"
    assert zero.expected_abs_move_bps is None
    assert zero.as_detail() == missing.as_detail()


def test_a_longer_holding_window_needs_a_bigger_move_to_clear_the_same_cost():
    """Cost is paid once; the window is what determines whether it was worth it."""

    features = {"atr_5m_bps": 12.0}
    short = gate().evaluate(features=features, horizon_seconds=15 * 60)
    long = gate().evaluate(features=features, horizon_seconds=240 * 60)

    assert short.expected_abs_move_bps < long.expected_abs_move_bps
    assert short.blocked is True
    assert long.blocked is False


def test_the_detail_is_flat_and_json_safe():
    detail = gate().evaluate(
        features={"atr_5m_bps": 12.0}, horizon_seconds=3600
    ).as_detail()

    assert detail["regime_blocked"] in (True, False)
    assert isinstance(detail["regime_expected_abs_move_bps"], float)
    assert isinstance(detail["regime_coverage"], float)
    assert detail["regime_horizon_minutes"] == 60
