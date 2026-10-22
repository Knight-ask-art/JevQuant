"""Where the time went between a candle closing and an order existing.

The pipeline is a chain of waits, and until now none of them were measured:
a signal is created, Jev is called, the gates run, the risk engine approves,
the adapter submits, the venue answers. When fills land late or prices drift
between approval and execution, the useful question is *which* stage was slow,
and the only way to answer it is to record each stage's boundary.

Two sources, deliberately:

- **The signal's own event trail** (``signal_events``) already timestamps every
  state change. Stages between two states are a subtraction, so most of this
  needs no new writes at all and works retroactively on recorded history.
- **The venue call** is the one boundary the state trail cannot see: a signal
  sits in ``EXECUTION_PENDING`` for both "the adapter is waiting on HTTP" and
  "the adapter is slow for some other reason". ``ExecutionReceipt.latency_ms``
  now carries the adapter's own measurement of that call.

What this report does not do: it does not tell anyone to tune a timeout, and
it does not compare against a target that was never agreed. It reports the
distribution and names the stage that dominates, which is a fact rather than a
recommendation.

Read-only. It reads persisted timestamps and never touches an order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any

from ..domain.enums import SignalState

#: Below this many samples a percentile is the sample, not the pipeline.
MIN_SAMPLES_FOR_EVIDENCE = 20

#: The stages the report measures, as (label, from-state, to-state). The order
#: is the pipeline's own order, so a gap shows up where it actually happened.
STAGES: tuple[tuple[str, str, str], ...] = (
    ("jev_evaluation", SignalState.CANDIDATE.value, SignalState.SCORED.value),
    ("gating", SignalState.SCORED.value, SignalState.ELIGIBLE.value),
    ("risk_approval", SignalState.RISK_PENDING.value, SignalState.APPROVED.value),
    ("dispatch", SignalState.APPROVED.value, SignalState.EXECUTION_PENDING.value),
    ("execution", SignalState.EXECUTION_PENDING.value, SignalState.ACCEPTED.value),
)

#: States that mean the same moment for timing purposes. A signal that fills
#: directly skips ACCEPTED; a shadowed order records ACCEPTED and stops there,
#: so the first of these reached is the end of the execution stage.
_TERMINAL_ALIASES: dict[str, tuple[str, ...]] = {
    SignalState.ACCEPTED.value: (
        SignalState.ACCEPTED.value,
        SignalState.PARTIALLY_FILLED.value,
        SignalState.FILLED.value,
        SignalState.POSITION_OPEN.value,
    ),
}


def _seconds(later: datetime | None, earlier: datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    delta = (later - earlier).total_seconds()
    return round(delta, 3) if delta >= 0 else None


def _median(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(median(present), 3)


def _p90(values: Sequence[float | None]) -> float | None:
    present = sorted(value for value in values if value is not None)
    if not present:
        return None
    # Nearest-rank: the smallest value that is at least the 90th percentile.
    index = min(len(present) - 1, max(0, int(round(0.9 * len(present))) - 1))
    return round(present[index], 3)


@dataclass(frozen=True)
class StageTiming:
    """One stage's samples, summarised."""

    stage: str
    samples: int
    median_seconds: float | None
    p90_seconds: float | None
    max_seconds: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "samples": self.samples,
            "median_seconds": self.median_seconds,
            "p90_seconds": self.p90_seconds,
            "max_seconds": self.max_seconds,
        }


@dataclass(frozen=True)
class ExecutionTelemetry:
    """Timings for one set of signals, plus the venue calls behind them."""

    signals: Sequence[Any]
    venue_latencies_ms: Sequence[float]

    def _boundaries(self, signal: Any) -> dict[str, datetime]:
        """First timestamp at which each state was reached."""

        found: dict[str, datetime] = {}
        for event in getattr(signal, "events", ()) or ():
            state = str(event.state)
            if state not in found:
                found[state] = event.created_at
        return found

    def stage_timings(self) -> list[StageTiming]:
        timings: list[StageTiming] = []
        for label, start, end in STAGES:
            samples: list[float | None] = []
            for signal in self.signals:
                marks = self._boundaries(signal)
                begin = marks.get(start)
                finish = next(
                    (marks[alias] for alias in _TERMINAL_ALIASES.get(end, (end,)) if alias in marks),
                    None,
                )
                value = _seconds(finish, begin)
                if value is not None:
                    samples.append(value)
            timings.append(
                StageTiming(
                    stage=label,
                    samples=len(samples),
                    median_seconds=_median(samples),
                    p90_seconds=_p90(samples),
                    max_seconds=round(max(samples), 3) if samples else None,
                )
            )
        return timings

    def venue_report(self) -> dict[str, Any]:
        """The adapter's own measurement of the exchange call.

        A signal that never reached an adapter has no sample, and that is not
        a zero: it is a missing measurement and is counted as such.
        """

        values = [value for value in self.venue_latencies_ms if value is not None]
        return {
            "calls": len(values),
            "median_ms": _median(values),
            "p90_ms": _p90(values),
            "max_ms": round(max(values), 1) if values else None,
        }

    def slowest_stage(self) -> str | None:
        """The stage with the largest median, or ``None`` without evidence.

        Ranked by median rather than max: one hung call is an incident, not a
        property of the pipeline, and the report is about the pipeline.
        """

        scored = [
            (timing.median_seconds, timing.stage)
            for timing in self.stage_timings()
            if timing.median_seconds is not None
        ]
        if not scored:
            return None
        return max(scored)[1]

    def adequacy(self) -> str:
        timed = [timing for timing in self.stage_timings() if timing.samples]
        if not timed:
            return "insufficient"
        smallest = min(timing.samples for timing in timed)
        return "adequate" if smallest >= MIN_SAMPLES_FOR_EVIDENCE else "insufficient"

    def report(self) -> dict[str, Any]:
        return {
            "signals_read": len(self.signals),
            "sample_adequacy": self.adequacy(),
            "stages": [timing.as_dict() for timing in self.stage_timings()],
            "venue": self.venue_report(),
            "slowest_stage": self.slowest_stage(),
            "note": (
                "Timings come from persisted state changes. A stage with few "
                "samples is a description of those samples, not of the pipeline."
            ),
        }


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"Signals read: {report['signals_read']}  "
        f"Sample adequacy: {report['sample_adequacy']}",
        "",
        "| stage | samples | median s | p90 s | max s |",
        "| --- | --- | --- | --- | --- |",
    ]
    for stage in report["stages"]:
        lines.append(
            f"| {stage['stage']} | {stage['samples']} | {stage['median_seconds']} | "
            f"{stage['p90_seconds']} | {stage['max_seconds']} |"
        )
    venue = report["venue"]
    lines.append("")
    lines.append(
        f"Venue calls measured: {venue['calls']}  median {venue['median_ms']} ms  "
        f"p90 {venue['p90_ms']} ms  max {venue['max_ms']} ms"
    )
    if report["slowest_stage"]:
        lines.append(f"Slowest stage by median: {report['slowest_stage']}")
    lines.append("")
    lines.append(report["note"])
    return "\n".join(lines)
