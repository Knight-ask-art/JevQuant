"""A statistical edge gate, run in shadow until it has earned the right to refuse.

The Jev gate answers "is this candidate good". It does not answer "have trades
like this, in this market, actually paid after costs" -- and Jev's probability
is a confidence over its own choices, not a probability of profit. Those are
different questions, and only the second one justifies an entry.

This module asks the second question the only way the data allows: look up the
**already-decided** outcomes of past candidates in the same cohort, and refuse
when the mean net return is not confidently above the cost of proving it.

Three properties make the answer hard to fool yourself with:

- **Past only.** A sample is eligible only when its holding window had closed
  by the decision instant -- not merely when it was decided earlier. Otherwise
  a trade from an hour ago whose 24 h window is still running would leak its
  future into today's verdict.
- **Pessimistic by construction.** A one-sided lower confidence bound on the
  mean net bps, with the missing-data rows (``no_data``) counted as ZERO rather
  than dropped. "We could not see the window" is not evidence of profit.
- **It can only refuse.** Like the regime gate, and unlike the risk engine,
  nothing here sizes or approves. It starts in shadow: the verdict is written
  down and audited, and the trade proceeds.

The textbook default of 100 samples per cohort is not reachable in weeks at
one Jev call per symbol per candle, so the threshold is a setting
(``edge_min_samples``) and the estimate reports its own uncertainty. Lowering
it is how a small sample is presented honestly -- not by dropping the bound.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import sqrt
from typing import Any

from ..domain.cohorts import confidence_class, symbol_class, volatility_class
from ..domain.money import ONE, ZERO, dec

ESTIMATOR = "cohort-mean-lower-bound-v1"
DEFAULT_MIN_SAMPLES = 30
DEFAULT_BOOTSTRAP_DRAWS = 2000
DEFAULT_CONFIDENCE = 0.95
DEFAULT_SAFETY_MARGIN_BPS = Decimal("0")
#: Minutes without a bar, beyond which a replay saw too little of the window to
#: be counted as an outcome rather than as missing data.
DEFAULT_MAX_GAP_MINUTES = 30
#: Samples read per cycle and per lookup. A bounded read; the cohort statistics
#: are what the verdict needs, not the whole table.
MAX_SAMPLES = 5000


@dataclass(frozen=True)
class CohortSample:
    """One past candidate that has fully played out."""

    cohort: str
    net_bps: Decimal | None
    decided_at: datetime
    holding_seconds: int
    gap_seconds: int
    method: str


@dataclass(frozen=True)
class EdgeEstimate:
    cohort: str
    sample_size: int
    expected_net_bps: Decimal
    lower_ci_bps: Decimal
    upper_ci_bps: Decimal
    required_net_bps: Decimal
    confidence: Decimal
    #: Rows in the cohort whose window was never seen. Counted as ZERO in the
    #: estimate and reported here, so the number is not silently buried.
    missing_outcomes: int = 0
    bootstrap_draws: int = 0

    def as_detail(self) -> dict[str, Any]:
        return {
            "edge_cohort": self.cohort,
            "edge_sample_size": self.sample_size,
            "edge_expected_net_bps": _round(self.expected_net_bps),
            "edge_lower_ci_bps": _round(self.lower_ci_bps),
            "edge_upper_ci_bps": _round(self.upper_ci_bps),
            "edge_required_net_bps": _round(self.required_net_bps),
            "edge_confidence": _round(self.confidence),
            "edge_missing_outcomes": self.missing_outcomes,
            "edge_estimator": ESTIMATOR,
        }


@dataclass(frozen=True)
class EdgeDecision:
    blocked: bool
    reason: str
    cohort: str
    estimate: EdgeEstimate | None
    #: Present only when there was no evidence: how much data was scanned at
    #: all, so "wait longer" can be told from "the lookup is broken".
    coverage: dict[str, int] | None = None

    def as_detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "edge_blocked": self.blocked,
            "edge_reason": self.reason,
            "edge_cohort": self.cohort,
        }
        if self.estimate is not None:
            detail.update(self.estimate.as_detail())
        elif self.coverage is not None:
            detail.update(
                {f"edge_{key}": value for key, value in self.coverage.items()}
            )
        return detail


def _round(value: Decimal | None) -> float | None:
    return None if value is None else round(float(value), 2)


def decide(
    *,
    cohort: str,
    samples: list[CohortSample],
    now: datetime,
    required_net_bps: Decimal,
    min_samples: int,
    confidence: Decimal,
    bootstrap_draws: int = DEFAULT_BOOTSTRAP_DRAWS,
) -> EdgeDecision:
    """The verdict for one cohort using only samples that had closed by ``now``.

    The lower bound comes from a bootstrap rather than the normal
    approximation: net returns from stop/target/timeout exits are lumpy, not
    bell-shaped, and the moments the normal formula needs are dominated by a
    few large losses. The bootstrap asks the question directly -- resample the
    cohort, recompute the mean, and see how often the mean would have landed
    below the required level. Its only randomness is a fixed seed, so the same
    record always yields the same verdict.
    """

    confidence = dec(confidence)
    required_net_bps = dec(required_net_bps)
    eligible: list[Decimal] = []
    missing = 0
    for sample in samples:
        if sample.cohort != cohort:
            continue
        elapsed = (now - sample.decided_at).total_seconds()
        if elapsed < sample.holding_seconds:
            # Still running: its outcome is partly in the future.
            continue
        if sample.net_bps is None:
            missing += 1
            eligible.append(ZERO)
            continue
        eligible.append(dec(sample.net_bps))

    n = len(eligible)
    if n < min_samples:
        return EdgeDecision(
            blocked=True,
            reason="insufficient_evidence",
            cohort=cohort,
            estimate=None,
            coverage={"eligible_samples": n, "missing_outcomes": missing},
        )

    mean = sum(eligible, ZERO) / Decimal(n)
    lower, upper = _bootstrap_interval(
        eligible, mean=mean, confidence=confidence, draws=bootstrap_draws
    )
    estimate = EdgeEstimate(
        cohort=cohort,
        sample_size=n,
        expected_net_bps=mean,
        lower_ci_bps=lower,
        upper_ci_bps=upper,
        required_net_bps=required_net_bps,
        confidence=confidence,
        missing_outcomes=missing,
        bootstrap_draws=bootstrap_draws,
    )
    if lower <= required_net_bps:
        return EdgeDecision(
            blocked=True,
            reason="edge_not_statistically_positive",
            cohort=cohort,
            estimate=estimate,
        )
    return EdgeDecision(
        blocked=False,
        reason="positive_conservative_edge",
        cohort=cohort,
        estimate=estimate,
    )


def _bootstrap_interval(
    values: list[Decimal],
    *,
    mean: Decimal,
    confidence: Decimal,
    draws: int,
) -> tuple[Decimal, Decimal]:
    """Percentile interval of the resampled mean.

    Fewer draws than the sample size cannot describe a distribution of means,
    so a cohort that barely clears the sample floor gets the degenerate
    interval ``[mean, mean]`` -- which is honest: at that size there is no
    interval to speak of, only the mean.
    """

    n = len(values)
    if draws <= 0 or n < 2:
        return mean, mean
    tail = (ONE - dec(confidence)) / Decimal("2")
    tail = min(max(float(tail), 0.0), 0.5)
    rng = random.Random(f"{ESTIMATOR}:{n}")  # fixed seed: verdicts must repeat
    sums = [0.0] * draws
    floats = [float(value) for value in values]
    for index in range(draws):
        sums[index] = sum(rng.choices(floats, k=n))
    sums.sort()
    lower_at = max(0, min(draws - 1, int(tail * draws)))
    upper_at = max(0, min(draws - 1, int((1.0 - tail) * draws) - 1))
    return (
        Decimal(str(sums[lower_at] / n)).quantize(Decimal("0.0001")),
        Decimal(str(sums[upper_at] / n)).quantize(Decimal("0.0001")),
    )


class EdgeEvaluator:
    """Looks up the cohort's past outcomes and returns the shadow verdict."""

    def __init__(
        self,
        *,
        repo: Any,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        confidence: Decimal = DEFAULT_CONFIDENCE,
        safety_margin_bps: Decimal = DEFAULT_SAFETY_MARGIN_BPS,
        max_gap_minutes: int = DEFAULT_MAX_GAP_MINUTES,
    ) -> None:
        self.repo = repo
        self.min_samples = min_samples
        self.confidence = confidence
        self.safety_margin_bps = safety_margin_bps
        self.max_gap_minutes = max_gap_minutes

    @classmethod
    def from_settings(cls, settings: Any, repo: Any) -> EdgeEvaluator:
        return cls(
            repo=repo,
            min_samples=int(getattr(settings, "edge_min_samples", DEFAULT_MIN_SAMPLES)),
            confidence=dec(getattr(settings, "edge_confidence", DEFAULT_CONFIDENCE)),
            safety_margin_bps=dec(
                getattr(settings, "edge_safety_margin_bps", DEFAULT_SAFETY_MARGIN_BPS)
            ),
            max_gap_minutes=int(
                getattr(settings, "edge_max_gap_minutes", DEFAULT_MAX_GAP_MINUTES)
            ),
        )

    def cohort_for(
        self,
        *,
        symbol: str,
        features: dict[str, float],
        probability: float,
        horizon_seconds: int,
        round_trip_cost_bps: Decimal,
    ) -> str:
        """The band this candidate belongs to.

        Cost is banded rather than compared numerically: a replay recorded
        under a different fee schedule is still comparable when the cost is
        similar, which is exactly what a numeric equality join would refuse.
        """

        cost_class = (
            "cost_low"
            if round_trip_cost_bps < Decimal("20")
            else "cost_mid"
            if round_trip_cost_bps < Decimal("40")
            else "cost_high"
        )
        return "|".join(
            (
                symbol_class(symbol),
                volatility_class(features.get("atr_5m_bps")),
                confidence_class(probability),
                f"h{max(0, horizon_seconds // 3600)}",
                cost_class,
            )
        )

    async def evaluate(
        self,
        *,
        signal_id: str,
        symbol: str,
        features: dict[str, float],
        probability: float,
        horizon_seconds: int,
        round_trip_cost_bps: Decimal,
        now: datetime,
    ) -> EdgeDecision:
        cohort = self.cohort_for(
            symbol=symbol,
            features=features,
            probability=probability,
            horizon_seconds=horizon_seconds,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        rows = await self.repo.list_edge_samples(limit=MAX_SAMPLES)
        # Read once per verdict, only to explain an empty cohort.
        coverage = await self.repo.counterfactual_lookup_counts()
        samples = [
            CohortSample(
                cohort=self.cohort_for(
                    symbol=row.symbol,
                    features=row.features,
                    probability=row.entry_probability,
                    horizon_seconds=row.horizon_seconds,
                    round_trip_cost_bps=row.round_trip_cost_bps,
                ),
                net_bps=row.net_bps,
                decided_at=row.decided_at,
                holding_seconds=row.horizon_seconds,
                gap_seconds=row.gap_seconds or 0,
                method=row.method,
            )
            for row in rows
            if (row.gap_seconds or 0) <= self.max_gap_minutes * 60
        ]
        decision = decide(
            cohort=cohort,
            samples=samples,
            now=now,
            required_net_bps=self.safety_margin_bps,
            min_samples=self.min_samples,
            confidence=self.confidence,
        )
        if decision.coverage is not None:
            decision = EdgeDecision(
                blocked=decision.blocked,
                reason=decision.reason,
                cohort=decision.cohort,
                estimate=None,
                coverage={**decision.coverage, **coverage},
            )
        await self.repo.insert_edge_shadow(
            signal_id=signal_id,
            cohort=cohort,
            decided_at=now,
            blocked=decision.blocked,
            reason=decision.reason,
            sample_size=decision.estimate.sample_size if decision.estimate else 0,
            expected_net_bps=decision.estimate.expected_net_bps if decision.estimate else None,
            lower_ci_bps=decision.estimate.lower_ci_bps if decision.estimate else None,
            upper_ci_bps=decision.estimate.upper_ci_bps if decision.estimate else None,
            required_net_bps=decision.estimate.required_net_bps if decision.estimate else None,
            confidence=decision.estimate.confidence if decision.estimate else None,
            estimator=ESTIMATOR,
        )
        return decision


def describe(decision: EdgeDecision) -> str:
    """One line for an audit entry or a cycle note."""

    if decision.estimate is None:
        scanned = (decision.coverage or {}).get("counterfactual_rows", 0)
        eligible = (decision.coverage or {}).get("eligible_samples", 0)
        return (
            f"{decision.reason}: cohort {decision.cohort} has {eligible} eligible "
            f"of {scanned} recorded outcomes"
        )
    estimate = decision.estimate
    return (
        f"{decision.reason}: cohort {decision.cohort} n={estimate.sample_size}, "
        f"mean {estimate.expected_net_bps:.2f} bps, "
        f"lower bound {estimate.lower_ci_bps:.2f} bps vs required "
        f"{estimate.required_net_bps:.2f} bps"
    )
