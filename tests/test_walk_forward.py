"""Walk-forward: the protocol is the point, so the protocol gets the tests.

A walk-forward can be wrong in ways that look like success -- a leaked sample
turns a losing segment into a winning one, a single lucky cell turns noise into
a parameter. These tests pin the defences rather than the numbers: the fold
layout, the purge, the plateau requirement, and that a random search over noise
does not get confirmed out of sample.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from jev_trading.domain.costs import CostModel
from jev_trading.services.walk_forward import (
    ParameterGrid,
    Sample,
    build_report,
    evaluate_grid,
    folds,
    net_bps,
    plateau,
    purge,
)

START = datetime(2026, 9, 1, tzinfo=UTC)
HORIZON = timedelta(hours=4)
COST = CostModel()


def grid() -> ParameterGrid:
    return ParameterGrid(
        round_trip_bps=COST.round_trip_bps,
        exit_slippage_bps=COST.exit_slippage_bps,
        fee_bps=COST.fee_bps,
    )


def sample(at: datetime, *, mfe: str = "60", mae: str = "-60", net: str = "10") -> Sample:
    entry = Decimal("100")
    return Sample(
        decided_at=at,
        symbol="BTCUSDT",
        entry_price=entry,
        entry_close=entry * (Decimal("1") + Decimal(net) / Decimal("10000")),
        exit_close=entry * (Decimal("1") + Decimal(net) / Decimal("10000")),
        entry_probability=0.7,
        mfe_bps=Decimal(mfe),
        mae_bps=Decimal(mae),
        recorded_exit_reason="timeout",
        window_minutes=240,
        round_trip_cost_bps=COST.round_trip_bps,
        recorded_net_bps=Decimal(net),
    )


# -- layout ------------------------------------------------------------------
def test_the_test_segments_are_disjoint_and_cover_the_range():
    """Every observation is tested on once; overlapping tests would double-count."""

    end = START + timedelta(days=28)

    loop = folds(start=START, end=end, count=4)

    assert [fold.index for fold in loop] == [0, 1, 2, 3]
    for earlier, later in zip(loop, loop[1:]):
        assert earlier.test[1] == later.test[0]
        # Training always precedes its own test: no fold trains on the future.
        assert earlier.train[0] <= earlier.validation[0] <= earlier.test[0]
    assert loop[-1].test[1] <= end


def test_a_fold_cannot_be_built_from_a_backwards_range():
    try:
        folds(start=START, end=START - timedelta(days=1), count=2)
    except ValueError:
        return
    raise AssertionError("a backwards range must be refused")


# -- the purge ---------------------------------------------------------------
def test_a_sample_still_running_at_the_boundary_is_dropped():
    """Its window would carry the future into the segment it entered."""

    segment = (START, START + timedelta(days=1))
    inside = sample(START + timedelta(hours=2))
    straddles_start = sample(START - timedelta(hours=3))
    straddles_end = sample(START + timedelta(hours=23))

    kept = purge([inside, straddles_start, straddles_end], segment=segment, horizon=HORIZON, embargo=timedelta(0))

    assert kept == [inside]


def test_the_embargo_is_applied_on_both_sides():
    segment = (START, START + timedelta(days=1))
    early = sample(START + timedelta(hours=1))
    late = sample(START + timedelta(hours=21))

    kept = purge([early, late], segment=segment, horizon=HORIZON, embargo=timedelta(hours=2))

    assert kept == []


# -- repricing ---------------------------------------------------------------
def test_a_target_pays_exit_slippage_and_both_taker_fees():
    """Entry slippage is in entry_price; the entry taker fee is not."""

    value = net_bps(
        sample(START, mfe="90", mae="-10"),
        stop_bps=Decimal("30"),
        take_bps=Decimal("60"),
        hold_minutes=60,
        exit_slippage_bps=COST.exit_slippage_bps,
        fee_bps=COST.fee_bps,
    )

    expected = ((Decimal("1") + Decimal("60") / 10000) * (Decimal("1") - COST.exit_slippage_bps / 10000) - 1) * 10000 - COST.fee_bps * 2
    assert value == expected


def test_a_bar_that_touched_both_levels_is_a_stop_as_in_the_recorder():
    value = net_bps(
        sample(START, mfe="200", mae="-200"),
        stop_bps=Decimal("30"),
        take_bps=Decimal("60"),
        hold_minutes=60,
        exit_slippage_bps=COST.exit_slippage_bps,
        fee_bps=COST.fee_bps,
    )

    expected = ((Decimal("1") - Decimal("30") / 10000) * (Decimal("1") - COST.exit_slippage_bps / 10000) - 1) * 10000 - COST.fee_bps * 2
    assert value == expected


# -- the plateau -------------------------------------------------------------
def _uniform_samples(count: int, *, atr_up: str = "120", atr_down: str = "-40") -> list[Sample]:
    return [
        sample(START + timedelta(hours=index), mfe=atr_up, mae=atr_down)
        for index in range(count)
    ]


def test_a_single_profitable_corner_is_not_a_plateau():
    """One good cell with losers around it is noise, however good the cell is."""

    cells = []
    for stop in (Decimal("15"), Decimal("30"), Decimal("45")):
        for take in (Decimal("30"), Decimal("45"), Decimal("60")):
            for hold in (30, 60, 120):
                good = stop == Decimal("30") and take == Decimal("60") and hold == 120
                cells.append(
                    type(
                        "Cell", (), {
                            "stop_bps": stop, "take_bps": take, "hold_minutes": hold,
                            "trades": 40, "mean_net_bps": Decimal("80") if good else Decimal("-20"),
                            "key": (stop, take, hold),
                        }
                    )()
                )

    assert plateau(cells, cost_bps=Decimal("30")) is None


def test_a_broad_neighbourhood_is_a_plateau():
    """Centres are interior cells, so the grid needs real volume around them."""

    samples = _uniform_samples(200)
    wide = ParameterGrid(
        round_trip_bps=COST.round_trip_bps,
        exit_slippage_bps=COST.exit_slippage_bps,
        fee_bps=COST.fee_bps,
        stop_multiples=tuple(Decimal(value) for value in ("0.5", "1", "1.5", "2", "2.5")),
        take_multiples=tuple(Decimal(value) for value in ("1", "1.5", "2", "3", "4")),
        holds_minutes=(30, 60, 120, 240, 480),
    )

    found = plateau(
        evaluate_grid(samples, grid=wide, min_trades=10),
        cost_bps=COST.round_trip_bps,
        grid=wide,
    )

    assert found is not None
    assert found["positive_neighbours"] >= 5
    assert found["train_mean_net_bps"] > float(COST.round_trip_bps)


def test_a_cohort_that_only_pays_the_cost_is_not_a_plateau():
    """Mean net above zero but below cost plus margin proves nothing."""

    samples = [sample(START + timedelta(hours=index), mfe="40", mae="-10") for index in range(200)]

    found = plateau(
        evaluate_grid(samples, grid=grid(), min_trades=10),
        cost_bps=COST.round_trip_bps,
    )

    assert found is None


def _with_edge(count: int, seed: int) -> list[Sample]:
    """A real edge: the upside is usually further away than the downside."""

    rng = random.Random(seed)
    return [
        sample(
            START + timedelta(hours=index),
            mfe=str(round(rng.uniform(80, 200), 2)),
            mae=str(round(-rng.uniform(10, 40), 2)),
            net=str(round(rng.uniform(0, 40), 2)),
        )
        for index in range(count)
    ]


def test_a_real_edge_is_confirmed_and_a_flat_one_is_not():
    """The filter has to let something through, or it proves nothing.

    Same machinery, same folds, same seeds -- only the path shape differs.
    """

    wide = ParameterGrid(
        round_trip_bps=COST.round_trip_bps,
        exit_slippage_bps=COST.exit_slippage_bps,
        fee_bps=COST.fee_bps,
        stop_multiples=tuple(Decimal(value) for value in ("0.5", "1", "1.5", "2", "2.5")),
        take_multiples=tuple(Decimal(value) for value in ("1", "1.5", "2", "3", "4")),
        holds_minutes=(30, 60, 120, 240, 480),
    )

    edge = build_report(
        _with_edge(600, seed=8),
        start=START,
        end=START + timedelta(days=28),
        horizon=HORIZON,
        cost=COST,
        fold_count=4,
        min_trades=10,
        grid=wide,
    )
    flat = build_report(
        [
            sample(START + timedelta(hours=index), mfe="60", mae="-60", net="0")
            for index in range(600)
        ],
        start=START,
        end=START + timedelta(days=28),
        horizon=HORIZON,
        cost=COST,
        fold_count=4,
        min_trades=10,
        grid=wide,
    )

    assert edge["verdict"] == "plateau_confirmed"
    assert flat["verdict"] != "plateau_confirmed"


# -- end to end --------------------------------------------------------------
def _noise(count: int, seed: int) -> list[Sample]:
    rng = random.Random(seed)
    return [
        sample(
            START + timedelta(hours=index),
            mfe=str(round(rng.uniform(0, 150), 2)),
            mae=str(round(-rng.uniform(0, 150), 2)),
            net=str(round(rng.uniform(-80, 80), 2)),
        )
        for index in range(count)
    ]


def test_noise_over_many_folds_does_not_get_confirmed():
    """Several seeds, not one: a single lucky seed would hide a leaky filter."""

    for seed in (1, 5, 11, 42, 99):
        report = build_report(
            _noise(600, seed=seed),
            start=START,
            end=START + timedelta(days=28),
            horizon=HORIZON,
            cost=COST,
            fold_count=4,
            min_trades=10,
        )

        assert report["verdict"] == "no_plateau", f"seed {seed} confirmed noise"


def test_too_few_samples_says_so_instead_of_guessing():
    report = build_report(
        _noise(12, seed=3),
        start=START,
        end=START + timedelta(days=28),
        horizon=HORIZON,
        cost=COST,
        fold_count=4,
        min_trades=10,
    )

    assert report["sample_adequacy"] == "insufficient"
    assert report["verdict"] == "no_plateau"
