"""Walk-forward evaluation: split time, keep it honest, then look for a plateau.

Walk-forward is the protocol, and the protocol is the point. Two things make a
walk-forward result worthless no matter how good the number looks:

- **Leakage.** A sample whose holding window runs past a boundary carries the
  future into the segment it entered. Every sample is therefore purged when its
  window (plus an embargo) would cross a boundary -- at either end.
- **One lucky cell.** A grid search over stop/take/hold that finds a single
  profitable corner has found noise. What matters is a *plateau*: a parameter
  whose neighbours are also profitable, at a margin that is not one lucky
  trade away from flipping.

So the output is not a "best parameter". It is, per fold, whether a plateau
exists at all, and what the chosen cell would have done out of sample.

Deliberate limits, stated so they are not mistaken for oversights:

- Windows are **calendar-based**: segments are equal parts of the requested
  range, not equal numbers of trades. A quiet stretch is thin evidence, and
  each fold reports its own sample counts so that shows.
- The unit is the **individual signal**, each judged alone. A portfolio
  (one position at a time) makes a segment's result depend on a tie-break
  order, which makes a greedy search over it meaningless.
- A sample belongs to the segment its **window end** falls in. Selection looks
  only at windows that had ended; the test segment then looks at the answer
  key. Walk-forward cannot run without the answer key -- what it can do is
  refuse to let the selection see it.
- The exit geometry is searched against *recorded* outcomes: a stop or target
  further out than the path ever reached cannot be re-derived from MFE/MAE, so
  those cells read as timeouts. The grid is centred on the round trip for that
  reason, and the range limits are reported next to the result.

This module computes numbers and a verdict. It never writes a parameter
anywhere: choosing one is a human decision made with the fold record in hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import product
from typing import Any

from ..domain.money import ZERO

#: Stop/take grid, as multiples of the round trip: the unit that matters. A
#: stop inside the cost is noise, a take under it cannot pay.
DEFAULT_STOP_MULTIPLES = (Decimal("0.5"), Decimal("1"), Decimal("1.5"), Decimal("2"))
DEFAULT_TAKE_MULTIPLES = (Decimal("1"), Decimal("1.5"), Decimal("2"), Decimal("3"))
#: Holding windows, in minutes.
DEFAULT_HOLDS_MINUTES = (30, 60, 120, 240)
#: The centre needs this many neighbours (of up to 8) that are also positive.
#: Below it the "plateau" is a spike.
DEFAULT_MIN_NEIGHBOURS = 5
#: A neighbour is positive only if its mean net clears the round trip by this
#: fraction. A cell that only pays the cost is not evidence of an edge.
DEFAULT_MARGIN_FRACTION = Decimal("0.25")
#: Fewest trades a cell or segment needs before its mean means anything.
DEFAULT_MIN_TRADES = 10


# -- samples -----------------------------------------------------------------
@dataclass(frozen=True)
class Sample:
    """One recorded candidate, as the replay left it.

    MFE/MAE say how far the observed path went each way, not their order.
    ``exit_close`` is the recorder's slipped exit fill price, which may be
    earlier than the horizon. It is not an alternative-horizon close.
    """

    decided_at: datetime
    symbol: str
    entry_price: Decimal
    entry_close: Decimal
    exit_close: Decimal
    entry_probability: float
    mfe_bps: Decimal
    mae_bps: Decimal
    recorded_exit_reason: str
    window_minutes: int
    round_trip_cost_bps: Decimal
    #: What the recorder says this candidate actually returned. Kept so a grid
    #: cell that matches the recorded geometry can be checked against it.
    recorded_net_bps: Decimal | None = None

    def quality(self) -> float:
        """How attractive the candidate looked at decision time.

        Built only from what was known then. Confidence is a model score and
        is kept, clearly labelled as such.
        """

        return float(self.entry_probability)


# -- repricing ---------------------------------------------------------------
def net_bps(
    sample: Sample,
    *,
    stop_bps: Decimal,
    take_bps: Decimal,
    hold_minutes: int,
    exit_slippage_bps: Decimal,
    fee_bps: Decimal,
) -> Decimal:
    """Modelled net bps under an alternative stop/take/hold geometry.

    ``entry_price`` already includes entry slippage, **not** the entry fee.
    For a hypothetical stop/target, apply exit slippage to the threshold price
    and subtract both taker fees. For a timeout, ``exit_close`` is the
    persisted, already-slipped exit price: do not charge slippage a second time.

    This is still an MFE/MAE approximation, not a path replay: the ordering of
    extremes and the price at a different holding horizon are not observable
    from the recorded outcome. ``hold_minutes`` is retained for the grid
    interface, but cannot produce an independently observed timeout price.
    """

    if hold_minutes != sample.window_minutes:
        raise ValueError("cannot reprice an alternative holding period from recorded MFE/MAE")
    entry = sample.entry_price
    if entry <= ZERO:
        return ZERO
    # MAE is negative. If both levels were touched, the stop wins, matching
    # the conservative convention in the counterfactual recorder.
    stop_hit = sample.mae_bps <= -stop_bps
    target_hit = sample.mfe_bps >= take_bps
    if stop_hit or target_hit:
        threshold_bps = -stop_bps if stop_hit else take_bps
        raw_exit = entry * (Decimal("1") + threshold_bps / Decimal("10000"))
        exit_price = raw_exit * (
            Decimal("1") - exit_slippage_bps / Decimal("10000")
        )
    else:
        # This is only the *recorded* exit, not the exit at hold_minutes.
        # The alternative horizon needs minute-bar replay to be valid.
        exit_price = sample.exit_close
    return (exit_price / entry - Decimal("1")) * Decimal("10000") - fee_bps * Decimal("2")


# -- grid --------------------------------------------------------------------
class ParameterGrid:
    """The cells a walk-forward searches: stop x take x hold."""

    def __init__(
        self,
        *,
        round_trip_bps: Decimal,
        exit_slippage_bps: Decimal,
        fee_bps: Decimal,
        stop_multiples: tuple[Decimal, ...] = DEFAULT_STOP_MULTIPLES,
        take_multiples: tuple[Decimal, ...] = DEFAULT_TAKE_MULTIPLES,
        holds_minutes: tuple[int, ...] = DEFAULT_HOLDS_MINUTES,
    ) -> None:
        if round_trip_bps <= ZERO:
            raise ValueError("round_trip_bps must be positive")
        self.round_trip_bps = round_trip_bps
        self.exit_slippage_bps = exit_slippage_bps
        self.fee_bps = fee_bps
        self.stop_multiples = stop_multiples
        self.take_multiples = take_multiples
        self.holds_minutes = holds_minutes

    def stops(self) -> list[Decimal]:
        return [self.round_trip_bps * multiple for multiple in self.stop_multiples]

    def takes(self) -> list[Decimal]:
        return [self.round_trip_bps * multiple for multiple in self.take_multiples]

    def cells(self) -> list[tuple[Decimal, Decimal, int]]:
        return list(product(self.stops(), self.takes(), self.holds_minutes))

    def as_dict(self) -> dict[str, Any]:
        return {
            "round_trip_bps": str(self.round_trip_bps),
            "stop_bps": [str(value) for value in self.stops()],
            "take_bps": [str(value) for value in self.takes()],
            "hold_minutes": list(self.holds_minutes),
            "cells": len(self.cells()),
        }


@dataclass(frozen=True)
class CellResult:
    stop_bps: Decimal
    take_bps: Decimal
    hold_minutes: int
    trades: int
    mean_net_bps: Decimal

    @property
    def key(self) -> tuple[Decimal, Decimal, int]:
        return (self.stop_bps, self.take_bps, self.hold_minutes)


def evaluate_grid(
    samples: list[Sample], *, grid: ParameterGrid, min_trades: int = DEFAULT_MIN_TRADES
) -> list[CellResult]:
    out: list[CellResult] = []
    if not samples:
        return out
    for stop, take, hold in grid.cells():
        eligible = [sample for sample in samples if sample.window_minutes == hold]
        values = [
            net_bps(
                sample,
                stop_bps=stop,
                take_bps=take,
                hold_minutes=hold,
                exit_slippage_bps=grid.exit_slippage_bps,
                fee_bps=grid.fee_bps,
            )
            for sample in eligible
        ]
        if len(values) < min_trades:
            continue
        out.append(
            CellResult(
                stop_bps=stop,
                take_bps=take,
                hold_minutes=hold,
                trades=len(values),
                mean_net_bps=sum(values, ZERO) / Decimal(len(values)),
            )
        )
    return out


def cell_values(
    samples: list[Sample], *, grid: ParameterGrid
) -> dict[tuple[Decimal, Decimal, int], list[Decimal]]:
    """Every sample's net bps under every cell, so regions can be pooled."""

    out: dict[tuple[Decimal, Decimal, int], list[Decimal]] = {}
    for stop, take, hold in grid.cells():
        out[(stop, take, hold)] = [
            net_bps(
                sample,
                stop_bps=stop,
                take_bps=take,
                hold_minutes=hold,
                exit_slippage_bps=grid.exit_slippage_bps,
                fee_bps=grid.fee_bps,
            )
            for sample in samples
            if sample.window_minutes == hold
        ]
    return out


def region_plan(
    cells: list[CellResult],
    *,
    base: CellResult,
    steps: int = 1,
    grid: ParameterGrid | None = None,
) -> list[CellResult]:
    """The ``base`` cell and every cell within ``steps`` of it on any axis."""

    stops = grid.stops() if grid else sorted({cell.stop_bps for cell in cells})
    takes = grid.takes() if grid else sorted({cell.take_bps for cell in cells})
    holds = list(grid.holds_minutes) if grid else sorted({cell.hold_minutes for cell in cells})
    axes = (stops, takes, holds)
    by_key = {cell.key: cell for cell in cells}
    plan = [base]
    for axis, values in enumerate(axes):
        position = values.index(base.key[axis])
        for offset in range(1, steps + 1):
            for direction in (-1, 1):
                index = position + direction * offset
                if 0 <= index < len(values):
                    key = list(base.key)
                    key[axis] = values[index]
                    peer = by_key.get(tuple(key))  # type: ignore[arg-type]
                    if peer is not None and peer not in plan:
                        plan.append(peer)
    return plan


def _is_interior(
    cell: CellResult,
    axes: tuple[list[Decimal], list[Decimal], list[int]],
    by_key: dict[tuple[Decimal, Decimal, int], CellResult],
) -> bool:
    """A cell with a neighbour on both sides of every axis.

    Grid edges are excluded from being centres: their neighbourhood is a wall,
    so "most neighbours are positive" is trivially true there, and a pooled
    region hanging off the edge of what was searched says little.
    """

    for axis, values in enumerate(axes):
        if len(values) == 1:
            continue
        position = values.index(cell.key[axis])
        for step in (-1, 1):
            index = position + step
            if not 0 <= index < len(values):
                return False
            key = list(cell.key)
            key[axis] = values[index]
            if tuple(key) not in by_key:  # type: ignore[arg-type]
                return False
    return True


def plateau(
    cells: list[CellResult],
    *,
    cost_bps: Decimal,
    grid: ParameterGrid | None = None,
    margin_fraction: Decimal = DEFAULT_MARGIN_FRACTION,
    min_neighbours: int = DEFAULT_MIN_NEIGHBOURS,
) -> dict[str, Any] | None:
    """The best cell whose neighbours are *also* positive, or ``None``.

    A neighbour differs in exactly one axis by one step. Requiring several of
    them to clear the margin is what separates a wide platform from a spike:
    the spike has a peer or two that happened to land nearby, the platform has
    most of its neighbourhood.
    """

    if not cells:
        return None
    required = cost_bps * (Decimal("1") + margin_fraction)
    # Axes come from the grid, not from the surviving cells: a cell that
    # dropped out for lack of samples would otherwise shift every index.
    stops = grid.stops() if grid is not None else sorted({cell.stop_bps for cell in cells})
    takes = grid.takes() if grid is not None else sorted({cell.take_bps for cell in cells})
    holds = list(grid.holds_minutes) if grid is not None else sorted({cell.hold_minutes for cell in cells})
    axes = (stops, takes, holds)
    active_axes = sum(1 for values in axes if len(values) > 1)
    required_neighbours = min(min_neighbours, max(1, 2 * active_axes - 1))
    by_key = {cell.key: cell for cell in cells}

    def neighbours(cell: CellResult) -> list[CellResult]:
        out: list[CellResult] = []
        for axis, values in enumerate(axes):
            if len(values) == 1:
                continue
            position = values.index(cell.key[axis])
            for step in (-1, 1):
                index = position + step
                if 0 <= index < len(values):
                    key = list(cell.key)
                    key[axis] = values[index]
                    peer = by_key.get(tuple(key))  # type: ignore[arg-type]
                    if peer is not None:
                        out.append(peer)
        return out

    # A cell is a plateau centre when most of the neighbours *that exist* are
    # positive. Comparing against a fixed count would let the edge of the grid
    # -- which has fewer neighbours -- never qualify, and absolute counts also
    # call a wide flat region a spike whenever its neighbours are positive but
    # below its own peak.
    interior = {cell.key for cell in cells if _is_interior(cell, axes, by_key)}

    best: dict[str, Any] | None = None
    for cell in cells:
        if cell.key not in interior or cell.mean_net_bps <= required:
            continue
        peers = neighbours(cell)
        positive = [peer for peer in peers if peer.mean_net_bps > required]
        if not peers or len(positive) * 2 < len(peers) or len(positive) < required_neighbours:
            continue
        if best is None or cell.mean_net_bps > best["_mean"]:
            best = {
                "stop_bps": float(cell.stop_bps),
                "take_bps": float(cell.take_bps),
                "hold_minutes": cell.hold_minutes,
                "train_mean_net_bps": round(float(cell.mean_net_bps), 2),
                "neighbours": len(peers),
                "positive_neighbours": len(positive),
                "trades": cell.trades,
                "_mean": cell.mean_net_bps,
            }
    if best is not None:
        best.pop("_mean", None)
    return best


# -- folds -------------------------------------------------------------------
@dataclass(frozen=True)
class Fold:
    index: int
    train: tuple[datetime, datetime]
    validation: tuple[datetime, datetime]
    test: tuple[datetime, datetime]


def folds(
    *,
    start: datetime,
    end: datetime,
    count: int = 4,
    train_parts: int = 3,
    validation_parts: int = 1,
    test_parts: int = 1,
) -> list[Fold]:
    """Rolling train/validation/test windows laid end to end.

    Each fold spans ``train + validation + test`` parts and the next one starts
    one ``test`` part later, so successive test segments are disjoint: every
    observation is tested on exactly once.
    """

    total_parts = train_parts + validation_parts + test_parts
    if count < 1 or total_parts < 3:
        raise ValueError("need at least one fold of three non-empty parts")
    span = end - start
    if span <= timedelta(0):
        raise ValueError("end must be after start")
    part = span / (count + total_parts - 1)
    out: list[Fold] = []
    for index in range(count):
        base = start + part * index
        train_end = base + part * train_parts
        validation_end = train_end + part * validation_parts
        test_end = validation_end + part * test_parts
        out.append(
            Fold(
                index=index,
                train=(base, train_end),
                validation=(train_end, validation_end),
                test=(validation_end, test_end),
            )
        )
    return out


def purge(
    samples: list[Sample],
    *,
    segment: tuple[datetime, datetime],
    horizon: timedelta,
    embargo: timedelta,
) -> list[Sample]:
    """Samples whose whole window sits inside ``segment``, embargo included.

    A sample decided just before the segment opens can still be running when
    it does; one decided just before it closes ends inside the *next* segment.
    Both would share a price path across a boundary, so both are dropped
    rather than trimmed.
    """

    begin, finish = segment
    lo = begin + embargo
    hi = finish - horizon - embargo
    if hi < lo:
        return []
    return [sample for sample in samples if lo <= sample.decided_at <= hi]


# -- the run -----------------------------------------------------------------
def run(
    samples: list[Sample],
    *,
    grid: ParameterGrid,
    start: datetime,
    end: datetime,
    horizon: timedelta,
    fold_count: int = 4,
    embargo: timedelta | None = None,
    min_trades: int = DEFAULT_MIN_TRADES,
    margin_fraction: Decimal = DEFAULT_MARGIN_FRACTION,
    min_neighbours: int = DEFAULT_MIN_NEIGHBOURS,
) -> dict[str, Any]:
    """Select on train, confirm on validation, report on the untouched test.

    Training works in two steps so a small sample still says something:

    1. every cell needs ``min_trades`` samples of its own;
    2. the best such cell that is also a *plateau centre* (most of its
       neighbours clear the cost-plus-margin bar) is picked, and its
       **region** -- itself and its one-step neighbours -- is pooled. The
       reported mean is the region's, because that is what a small sample can
       support; a single corner of it cannot.
    """

    embargo = embargo if embargo is not None else timedelta(0)
    cost = grid.round_trip_bps
    records: list[dict[str, Any]] = []
    for fold in folds(start=start, end=end, count=fold_count):
        train = purge(samples, segment=fold.train, horizon=horizon, embargo=embargo)
        validation = purge(
            samples, segment=fold.validation, horizon=horizon, embargo=embargo
        )
        test = purge(samples, segment=fold.test, horizon=horizon, embargo=embargo)
        values = cell_values(train, grid=grid) if train else {}
        cells = [
            CellResult(
                stop_bps=stop,
                take_bps=take,
                hold_minutes=hold,
                trades=len(rows),
                mean_net_bps=sum(rows, ZERO) / Decimal(len(rows)),
            )
            for (stop, take, hold), rows in values.items()
            if len(rows) >= min_trades
        ]
        centre = plateau(
            cells,
            cost_bps=cost,
            grid=grid,
            margin_fraction=margin_fraction,
            min_neighbours=min_neighbours,
        )
        plan: list[CellResult] = []
        if centre is not None:
            base = next(
                cell
                for cell in cells
                if (float(cell.stop_bps), float(cell.take_bps), cell.hold_minutes)
                == (centre["stop_bps"], centre["take_bps"], centre["hold_minutes"])
            )
            plan = region_plan(cells, base=base, grid=grid)
            centre["region_cells"] = len(plan)
            centre["region_trades"] = sum(len(values[cell.key]) for cell in plan)
        record: dict[str, Any] = {
            "fold": fold.index,
            "train": [item.isoformat() for item in fold.train],
            "validation": [item.isoformat() for item in fold.validation],
            "test": [item.isoformat() for item in fold.test],
            "samples": {
                "train": len(train),
                "validation": len(validation),
                "test": len(test),
            },
            "holding_periods_observed": sorted({s.window_minutes for s in train}),
            "unobserved_grid_holds": sorted(
                set(grid.holds_minutes) - {s.window_minutes for s in train}
            ),
        }
        if centre is None:
            record["verdict"] = "no_plateau"
            records.append(record)
            continue
        def segment_mean(rows: list[Sample]) -> float | None:
            if not rows:
                return None
            pooled: list[Decimal] = []
            for cell in plan:
                pooled.extend(
                    net_bps(
                        row,
                        stop_bps=cell.stop_bps,
                        take_bps=cell.take_bps,
                        hold_minutes=cell.hold_minutes,
                        exit_slippage_bps=grid.exit_slippage_bps,
                        fee_bps=grid.fee_bps,
                    )
                    for row in rows
                    if row.window_minutes == cell.hold_minutes
                )
            if not pooled:
                return None
            return round(float(sum(pooled, ZERO) / Decimal(len(pooled))), 2)

        validation_mean = segment_mean(validation)
        test_mean = segment_mean(test)
        record.update(centre)
        record["validation_mean_net_bps"] = validation_mean
        record["test_mean_net_bps"] = test_mean
        confirmed = (
            validation_mean is not None
            and validation_mean > float(cost)
            and test_mean is not None
            and test_mean > float(cost)
        )
        record["verdict"] = (
            "confirmed_out_of_sample" if confirmed else "failed_out_of_sample"
        )
        records.append(record)

    evaluated = [item for item in records if item["verdict"] != "no_plateau"]
    confirmed = [item for item in evaluated if item["verdict"] == "confirmed_out_of_sample"]
    span_days = max((end - start).total_seconds() / 86400.0, 0.0)
    needed = max(min_trades * fold_count * 2, 1)
    if not evaluated:
        verdict = "no_plateau"
    elif confirmed and len(confirmed) >= max(1, fold_count // 2):
        verdict = "plateau_confirmed"
    else:
        verdict = "not_confirmed"
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": round(span_days, 2)},
        "samples": len(samples),
        # Enough calendar time and enough trades are different questions; a
        # confirmed plateau on a handful of trades is still noise.
        "sample_adequacy": "ok" if len(samples) >= needed else "insufficient",
        "fold_record": records,
        "verdict": verdict,
        "grid": grid.as_dict(),
        "repricing_quality": "mfe_mae_approximation_not_path_replay",
        "decision_grade": False,
        "holding_period_assumption": "only_recorded_horizons_are_repriced",
    }


# -- the composed report -----------------------------------------------------
def build_report(
    samples: list[Sample],
    *,
    start: datetime,
    end: datetime,
    horizon: timedelta,
    cost: Any,
    fold_count: int,
    min_trades: int,
    grid: ParameterGrid | None = None,
) -> dict[str, Any]:
    """Fold record plus the grid and cost model it was produced under."""

    grid = grid or ParameterGrid(
        round_trip_bps=cost.round_trip_bps,
        exit_slippage_bps=cost.exit_slippage_bps,
        fee_bps=cost.fee_bps,
    )
    report = run(
        samples,
        grid=grid,
        start=start,
        end=end,
        horizon=horizon,
        fold_count=fold_count,
        min_trades=min_trades,
    )
    report["cost_model"] = cost.as_dict()
    return report


def format_report(report: dict[str, Any]) -> str:
    window = report["window"]
    lines = [
        f"Walk-forward: {window['start'][:10]} -> {window['end'][:10]} "
        f"({window['days']} days), {report['samples']} samples",
        f"Verdict: {report['verdict']}  (sample adequacy: {report['sample_adequacy']})",
        "",
        "| fold | train | validation | test | samples t/v/test | verdict | "
        "stop | take | hold | validation bps | test bps |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for fold in report["fold_record"]:
        counts = fold["samples"]
        lines.append(
            "| {f} | {t0}..{t1} | {v0}..{v1} | {s0}..{s1} | {tc}/{vc}/{sc} | {verdict} | "
            "{stop} | {take} | {hold} | {vb} | {sb} |".format(
                f=fold["fold"],
                t0=fold["train"][0][:10],
                t1=fold["train"][1][:10],
                v0=fold["validation"][0][:10],
                v1=fold["validation"][1][:10],
                s0=fold["test"][0][:10],
                s1=fold["test"][1][:10],
                tc=counts["train"],
                vc=counts["validation"],
                sc=counts["test"],
                verdict=fold["verdict"],
                stop=fold.get("stop_bps", "-"),
                take=fold.get("take_bps", "-"),
                hold=fold.get("hold_minutes", "-"),
                vb=fold.get("validation_mean_net_bps", "-"),
                sb=fold.get("test_mean_net_bps", "-"),
            )
        )
    grid = report["grid"]
    lines.append("")
    lines.append(
        f"Grid: {grid['cells']} cells, stops {grid['stop_bps']} bps, "
        f"takes {grid['take_bps']} bps, holds {grid['hold_minutes']} min"
    )
    if report["sample_adequacy"] != "ok":
        lines.append(
            "Not enough samples for a verdict yet: the fold record describes the "
            "procedure, not the strategy."
        )
    return "\n".join(lines)
