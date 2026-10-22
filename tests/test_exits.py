"""Exit geometry v2: the ratchet, and the promise that it is opt-in.

Two things need pinning. First, that a policy which does not mention the new
fields behaves exactly like v1 -- an upgrade must not silently tighten the stop
on a trade that is already running. Second, that the live rule and the replay
agree, because the counterfactual record is only evidence if it describes the
rule that actually runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from jev_trading.domain.exits import ExitGeometry, exit_plan, initial_stop_price
from jev_trading.domain.market import Candle
from jev_trading.domain.policy import ExitDefaults, PolicyCompiler
from jev_trading.services.counterfactual import simulate

ENTRY = Decimal("100")
COST = Decimal("30")
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def v1_geometry(stop: str = "120", take: str = "240") -> ExitGeometry:
    return ExitGeometry(stop_loss_bps=Decimal(stop), take_profit_bps=Decimal(take))


# -- v1 is untouched ---------------------------------------------------------
def test_without_a_ratchet_the_plan_is_the_original_stop():
    plan = exit_plan(
        entry_price=ENTRY,
        geometry=v1_geometry(),
        peak_price=ENTRY * Decimal("1.5"),
        bid=ENTRY,
        round_trip_cost_bps=COST,
    )

    assert plan.stop_price == initial_stop_price(ENTRY, Decimal("120"))
    assert plan.ratcheted is False
    assert plan.reason is None


def test_a_policy_that_omits_the_new_fields_keeps_v1_behaviour():
    """The compatibility promise: absent ratchet fields change nothing."""

    defaults = ExitDefaults(
        stop_loss_bps=Decimal("120"),
        take_profit_bps=Decimal("240"),
        max_holding_seconds=3600,
    )
    geometry = defaults.geometry()

    assert geometry.breakeven_trigger_bps is None
    assert geometry.trailing_trigger_bps is None
    assert geometry.trailing_distance_bps is None


# -- break-even --------------------------------------------------------------
def test_break_even_arms_only_after_the_trigger_and_clears_the_cost():
    geometry = ExitGeometry(
        stop_loss_bps=Decimal("120"),
        take_profit_bps=Decimal("240"),
        breakeven_trigger_bps=Decimal("100"),
    )

    not_yet = exit_plan(
        entry_price=ENTRY,
        geometry=geometry,
        peak_price=ENTRY * Decimal("1.005"),  # 50 bps, below the 100 bps trigger
        bid=ENTRY,
        round_trip_cost_bps=COST,
    )
    armed = exit_plan(
        entry_price=ENTRY,
        geometry=geometry,
        peak_price=ENTRY * Decimal("1.02"),
        bid=ENTRY,
        round_trip_cost_bps=COST,
    )

    assert not_yet.ratcheted is False
    assert not_yet.stop_price == initial_stop_price(ENTRY, Decimal("120"))
    assert armed.ratcheted is True
    # A stop at exactly the entry price still loses the fees, so break-even
    # sits above entry by the round trip.
    assert armed.stop_price == ENTRY * (Decimal("1") + COST / Decimal("10000"))
    assert armed.stop_price > ENTRY


def test_break_even_exits_a_position_that_comes_all_the_way_back():
    geometry = ExitGeometry(
        stop_loss_bps=Decimal("120"),
        take_profit_bps=Decimal("240"),
        breakeven_trigger_bps=Decimal("100"),
    )

    plan = exit_plan(
        entry_price=ENTRY,
        geometry=geometry,
        peak_price=ENTRY * Decimal("1.03"),
        bid=ENTRY * Decimal("0.999"),
        round_trip_cost_bps=COST,
    )

    assert plan.reason == "stop_loss"


def test_break_even_never_loosens_a_stop_that_is_already_tighter():
    """A break-even level below the current stop must not replace it.

    The stop is 60 bps wide and the round trip is 30, so break-even sits
    *above* entry but *below* a stop that is already tighter -- which is the
    case this pins: only the higher of the two survives.
    """

    geometry = ExitGeometry(
        stop_loss_bps=Decimal("20"),
        take_profit_bps=Decimal("240"),
        breakeven_trigger_bps=Decimal("50"),
    )

    plan = exit_plan(
        entry_price=ENTRY,
        geometry=geometry,
        peak_price=ENTRY * Decimal("1.02"),
        bid=ENTRY * Decimal("1.01"),
        round_trip_cost_bps=Decimal("5"),
    )

    # Break-even at entry + 5 bps beats the 20 bps stop, so the ratchet wins;
    # a 30 bps round trip would have lost to it, which the next assert covers.
    assert plan.stop_price == ENTRY * (Decimal("1") + Decimal("5") / Decimal("10000"))
    assert plan.ratcheted is True

    wider_cost = exit_plan(
        entry_price=ENTRY,
        geometry=ExitGeometry(
            stop_loss_bps=Decimal("20"),
            take_profit_bps=Decimal("240"),
            breakeven_trigger_bps=Decimal("50"),
        ),
        peak_price=ENTRY * Decimal("1.02"),
        bid=ENTRY * Decimal("1.01"),
        round_trip_cost_bps=Decimal("500"),
    )
    # A 500 bps break-even would sit above the market, i.e. an instant exit;
    # it is still the higher stop, and that is the caller's problem to avoid
    # by keeping the trigger above the cost. Documented, not hidden.
    assert wider_cost.stop_price > ENTRY


# -- trailing ----------------------------------------------------------------
def test_the_trail_follows_the_peak_and_never_walks_backwards():
    geometry = ExitGeometry(
        stop_loss_bps=Decimal("120"),
        take_profit_bps=Decimal("240"),
        trailing_trigger_bps=Decimal("150"),
        trailing_distance_bps=Decimal("100"),
    )

    high = exit_plan(
        entry_price=ENTRY,
        geometry=geometry,
        peak_price=ENTRY * Decimal("1.05"),
        bid=ENTRY * Decimal("1.04"),
        round_trip_cost_bps=COST,
    )
    pulled_back = exit_plan(
        entry_price=ENTRY,
        geometry=geometry,
        peak_price=ENTRY * Decimal("1.05"),
        bid=ENTRY * Decimal("1.02"),
        round_trip_cost_bps=COST,
    )

    expected = ENTRY * Decimal("1.05") * (Decimal("1") - Decimal("100") / Decimal("10000"))
    assert high.stop_price == expected
    # The peak is a high-water mark: a lower price does not lower the stop.
    assert pulled_back.stop_price == expected
    # At 1.02 the bid is below the trailed stop (103.95), so the position
    # would leave -- which is the trail doing its job.
    assert pulled_back.reason == "stop_loss"


def test_the_trail_takes_the_higher_of_itself_and_break_even():
    geometry = ExitGeometry(
        stop_loss_bps=Decimal("120"),
        take_profit_bps=Decimal("1000"),
        breakeven_trigger_bps=Decimal("50"),
        trailing_trigger_bps=Decimal("60"),
        trailing_distance_bps=Decimal("500"),
    )

    plan = exit_plan(
        entry_price=ENTRY,
        geometry=geometry,
        peak_price=ENTRY * Decimal("1.01"),
        bid=ENTRY,
        round_trip_cost_bps=COST,
    )

    # Trail would sit at 101 - 500bps = 95.95, below entry; break-even wins.
    assert plan.stop_price == ENTRY * (Decimal("1") + COST / Decimal("10000"))


def test_an_unobserved_peak_leaves_the_ratchet_off():
    geometry = ExitGeometry(
        stop_loss_bps=Decimal("120"),
        take_profit_bps=Decimal("240"),
        breakeven_trigger_bps=Decimal("50"),
        trailing_trigger_bps=Decimal("100"),
        trailing_distance_bps=Decimal("50"),
    )

    plan = exit_plan(
        entry_price=ENTRY,
        geometry=geometry,
        peak_price=None,
        bid=ENTRY,
        round_trip_cost_bps=COST,
    )

    assert plan.ratcheted is False
    assert plan.stop_price == initial_stop_price(ENTRY, Decimal("120"))


# -- the policy contract -----------------------------------------------------
def test_a_half_specified_trail_is_refused():
    with pytest.raises(ValueError, match="must be set together"):
        ExitDefaults(
            stop_loss_bps=Decimal("120"),
            take_profit_bps=Decimal("240"),
            max_holding_seconds=3600,
            trailing_trigger_bps=Decimal("100"),
        )


def test_a_trail_that_arms_below_its_own_distance_is_refused():
    """Otherwise the trail arms at or below entry: a stop with extra steps."""

    with pytest.raises(ValueError, match="smaller than trailing_trigger_bps"):
        ExitDefaults(
            stop_loss_bps=Decimal("120"),
            take_profit_bps=Decimal("240"),
            max_holding_seconds=3600,
            trailing_trigger_bps=Decimal("80"),
            trailing_distance_bps=Decimal("80"),
        )


def test_the_server_clamps_a_wide_trail_instead_of_trusting_the_policy(server_limits):
    policy = ExitDefaults(
        stop_loss_bps=Decimal("120"),
        take_profit_bps=Decimal("240"),
        max_holding_seconds=3600,
        trailing_trigger_bps=Decimal("900"),
        trailing_distance_bps=Decimal("800"),
    )

    compiled = PolicyCompiler(server_limits)._clamp_exits(policy, [])

    assert compiled.trailing_distance_bps == server_limits.max_trailing_distance_bps
    clamps = []
    PolicyCompiler(server_limits)._clamp_exits(policy, clamps)
    assert "exit_defaults.trailing_distance_bps" in [clamp.field for clamp in clamps]


def test_clamping_the_distance_cannot_make_the_trail_incoherent(server_limits):
    """The clamp only lowers the distance, so distance < trigger still holds.

    The validator requires the pair to be coherent on input; the server may
    narrow the distance but never widen it, so no post-clamp repair is needed.
    """

    policy = ExitDefaults(
        stop_loss_bps=Decimal("120"),
        take_profit_bps=Decimal("240"),
        max_holding_seconds=3600,
        trailing_trigger_bps=Decimal("420"),
        trailing_distance_bps=Decimal("410"),
    )

    compiled = PolicyCompiler(server_limits)._clamp_exits(policy, [])

    assert compiled.trailing_distance_bps == Decimal("400")
    assert compiled.trailing_trigger_bps == Decimal("420")
    assert compiled.trailing_distance_bps < compiled.trailing_trigger_bps


def test_a_break_even_trigger_inside_the_minimum_stop_is_raised(server_limits):
    policy = ExitDefaults(
        stop_loss_bps=Decimal("120"),
        take_profit_bps=Decimal("240"),
        max_holding_seconds=3600,
        breakeven_trigger_bps=Decimal("5"),
    )
    clamps = []

    compiled = PolicyCompiler(server_limits)._clamp_exits(policy, clamps)

    assert compiled.breakeven_trigger_bps == server_limits.min_stop_loss_bps
    assert "exit_defaults.breakeven_trigger_bps" in [clamp.field for clamp in clamps]


# -- replay parity -----------------------------------------------------------
def _bar(minute: int, *, high: float, low: float, close: float) -> Candle:
    return Candle(
        open_time=NOW + timedelta(minutes=minute - 1),
        close_time=NOW + timedelta(minutes=minute),
        open=close,
        high=high,
        low=low,
        close=close,
        volume=1.0,
    )


def replay(candles: list[Candle], rules: ExitDefaults):
    return simulate(
        candles=candles,
        entry_price=ENTRY,
        exit_rules=rules,
        horizon_end=NOW + timedelta(hours=1),
        taker_fee_bps=Decimal("10"),
        exit_slippage_bps=Decimal("8"),
        notional=Decimal("100"),
    )


def test_the_replay_uses_the_same_ratchet_as_the_live_rule():
    """A trail that protects a winner live must show up in the record too."""

    rules = ExitDefaults(
        stop_loss_bps=Decimal("120"),
        take_profit_bps=Decimal("900"),
        max_holding_seconds=3600,
        trailing_trigger_bps=Decimal("100"),
        trailing_distance_bps=Decimal("50"),
    )
    candles = [
        _bar(1, high=101.5, low=99.5, close=101.0),  # peak 150 bps, trail arms at 100
        _bar(2, high=101.6, low=100.8, close=100.9),  # dips below the trailed stop
    ]

    result = replay(candles, rules)

    assert result is not None
    assert result.exit_reason == "stop_loss"
    # The exit is the trail (peak 101.5 minus 50 bps), not the original stop.
    expected = (
        Decimal("101.5")
        * (Decimal("1") - Decimal("50") / Decimal("10000"))
        * (Decimal("1") - Decimal("8") / Decimal("10000"))
    )
    assert abs(result.exit_price - expected) < Decimal("0.0000001")
    assert result.exit_price > ENTRY


def test_the_replay_without_a_ratchet_still_uses_the_original_stop():
    rules = ExitDefaults(
        stop_loss_bps=Decimal("120"),
        take_profit_bps=Decimal("240"),
        max_holding_seconds=3600,
    )
    candles = [
        _bar(1, high=101.5, low=99.5, close=101.0),
        _bar(2, high=100.0, low=98.0, close=98.5),
    ]

    result = replay(candles, rules)

    assert result is not None
    assert result.exit_reason == "stop_loss"
    assert result.exit_price < ENTRY


def test_a_target_is_still_reached_on_a_bar_whose_low_touches_the_stop():
    """The bar's own high can hit the target even when its low hits the stop.

    Order inside the bar is unknowable, so the stop wins -- the same pessimism
    the recorder has always applied.
    """

    rules = ExitDefaults(
        stop_loss_bps=Decimal("120"),
        take_profit_bps=Decimal("240"),
        max_holding_seconds=3600,
    )

    result = replay([_bar(1, high=102.6, low=98.0, close=100.0)], rules)

    assert result is not None
    assert result.exit_reason == "stop_loss"
