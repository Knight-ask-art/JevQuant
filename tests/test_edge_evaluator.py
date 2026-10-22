"""The shadow edge gate: can a cohort's past pay for its next trade?

The evaluator is the one component whose correctness cannot be checked by
looking at its output -- a confident verdict from a leaky sample looks exactly
like a confident verdict from a clean one. These tests therefore pin the two
properties the verdict depends on, each with a case that fails if it is
dropped: only outcomes that had fully closed count, and missing windows count
as zero rather than disappearing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from jev_trading.services.edge_evaluator import (
    CohortSample,
    EdgeEvaluator,
    decide,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def sample(
    net_bps: str | None,
    *,
    hours_ago: float = 10,
    holding_seconds: int = 3600,
    cohort: str = "C",
    gap_seconds: int = 0,
) -> CohortSample:
    return CohortSample(
        cohort=cohort,
        net_bps=None if net_bps is None else Decimal(net_bps),
        decided_at=NOW - timedelta(hours=hours_ago),
        holding_seconds=holding_seconds,
        gap_seconds=gap_seconds,
        method="1m-conservative-v1",
    )


def verdict(samples, *, min_samples: int = 10, required: str = "0") -> object:
    return decide(
        cohort="C",
        samples=samples,
        now=NOW,
        required_net_bps=Decimal(required),
        min_samples=min_samples,
        confidence=Decimal("0.95"),
    )


# -- the look-ahead guard ----------------------------------------------------
def test_a_sample_whose_holding_window_is_still_open_is_not_used():
    """Decided an hour ago with a 24 h window: its outcome is still the future.

    Filtering by decision time instead of window end would let roughly a day
    of a sample's own future into the verdict that is supposed to predict it.
    """

    young = [sample("50", hours_ago=1, holding_seconds=24 * 3600)] * 20
    mature = [sample("50", hours_ago=30, holding_seconds=24 * 3600)] * 20

    assert verdict(young).reason == "insufficient_evidence"
    assert verdict(young).coverage["eligible_samples"] == 0
    assert verdict(mature).reason == "positive_conservative_edge"


def test_a_sample_counts_once_its_window_has_closed_exactly():
    exactly_closed = [sample("20", hours_ago=1, holding_seconds=3600)] * 10

    assert verdict(exactly_closed).estimate.sample_size == 10
    assert verdict(exactly_closed).reason == "positive_conservative_edge"


# -- what the sample is worth ------------------------------------------------
def test_a_cohort_that_loses_on_average_is_refused():
    assert verdict([sample("-8")] * 20).reason == "edge_not_statistically_positive"


def test_a_noisy_cohort_that_is_only_slightly_positive_fails_the_bound():
    """Mean above zero, lower bound above it too little to rely on."""

    noisy = [sample("60"), sample("-50")] * 10
    decision = verdict(noisy, required="0", min_samples=10)

    assert decision.estimate.expected_net_bps > 0
    assert decision.estimate.lower_ci_bps <= 0
    assert decision.reason == "edge_not_statistically_positive"


def test_a_cohort_that_pays_clearly_is_not_blocked():
    decision = verdict([sample("40")] * 20)
    assert decision.reason == "positive_conservative_edge"
    assert decision.estimate.sample_size == 20


def test_a_margin_makes_a_borderline_cohort_fail():
    """The same cohort passes at 0 bps and fails at a 45 bps requirement."""

    samples = [sample("40")] * 20

    assert verdict(samples).blocked is False
    assert verdict(samples, required="45").blocked is True


# -- missing data ------------------------------------------------------------
def test_a_window_with_no_data_counts_as_zero_not_as_excluded():
    """Dropping what could not be seen makes a cohort look better than it is."""

    seen = [sample("40")] * 20
    undecided = [sample(None)] * 20

    with_missing = verdict(seen + undecided)
    without = verdict(seen)

    assert with_missing.estimate.sample_size == 40
    assert with_missing.estimate.expected_net_bps == Decimal("20")
    assert with_missing.estimate.expected_net_bps < without.estimate.expected_net_bps
    # Still positive, but only half of what dropping the gaps would claim.
    assert without.estimate.expected_net_bps == Decimal("40")


def test_all_missing_is_a_zero_mean_and_not_a_pass():
    decision = verdict([sample(None)] * 20)
    assert decision.estimate.sample_size == 20
    assert decision.estimate.expected_net_bps == Decimal("0")
    assert decision.blocked is True


# -- the gate itself ---------------------------------------------------------
def test_below_the_minimum_sample_is_insufficient_not_approved():
    decision = verdict([sample("100")] * 9, min_samples=10)
    assert decision.reason == "insufficient_evidence"
    assert decision.estimate is None
    assert decision.coverage["eligible_samples"] == 9


def test_another_cohorts_samples_are_never_borrowed():
    others = [sample("100", cohort="OTHER")] * 30
    assert verdict(others).reason == "insufficient_evidence"


class _Repo:
    def __init__(self, rows, counts=None):
        self.rows = rows
        self.counts = counts or {"counterfactual_rows": len(rows), "decided_rows": len(rows)}
        self.inserted = []

    async def list_edge_samples(self, limit=5000):
        return self.rows

    async def counterfactual_lookup_counts(self):
        return self.counts

    async def insert_edge_shadow(self, **fields):
        self.inserted.append(fields)


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def row(symbol="BTCUSDT", atr=20.0, prob=0.7, horizon=3600, cost="30", net="-10", gap=0):
    return _Row(
        symbol=symbol,
        features={"atr_5m_bps": atr},
        entry_probability=prob,
        horizon_seconds=horizon,
        round_trip_cost_bps=Decimal(cost),
        net_bps=None if net is None else Decimal(net),
        gap_seconds=gap,
        method="1m-conservative-v1",
        decided_at=NOW - timedelta(hours=3),
    )


async def test_the_evaluator_writes_the_uninformative_verdicts_too():
    """'No evidence yet' is a cohort that must be countable, not a gap."""

    repo = _Repo([], counts={"counterfactual_rows": 0, "decided_rows": 0})
    evaluator = EdgeEvaluator(repo=repo, min_samples=10)

    decision = await evaluator.evaluate(
        signal_id="s1",
        symbol="BTCUSDT",
        features={"atr_5m_bps": 20.0},
        probability=0.7,
        horizon_seconds=3600,
        round_trip_cost_bps=Decimal("30"),
        now=NOW,
    )

    assert decision.blocked is True
    assert decision.reason == "insufficient_evidence"
    assert repo.inserted[0]["signal_id"] == "s1"
    assert repo.inserted[0]["blocked"] is True
    assert repo.inserted[0]["sample_size"] == 0


async def test_a_cohort_is_built_from_the_features_not_stored_with_the_trade():
    """The same snapshot shape must land in the same cohort on both sides."""

    repo = _Repo([row()] * 12)
    evaluator = EdgeEvaluator(repo=repo, min_samples=10)
    cohort = evaluator.cohort_for(
        symbol="BTCUSDT",
        features={"atr_5m_bps": 20.0},
        probability=0.7,
        horizon_seconds=3600,
        round_trip_cost_bps=Decimal("30"),
    )

    assert cohort == "BTCUSDT|normal|mid|h1|cost_mid"
    decision = await evaluator.evaluate(
        signal_id="s2",
        symbol="BTCUSDT",
        features={"atr_5m_bps": 20.0},
        probability=0.7,
        horizon_seconds=3600,
        round_trip_cost_bps=Decimal("30"),
        now=NOW,
    )
    assert decision.cohort == cohort
    assert decision.estimate.sample_size == 12
    assert decision.blocked is True  # every peer lost 10 bps


async def test_rows_whose_window_was_mostly_gaps_are_not_evidence():
    """A replay that saw a few minutes of a day-long window proves little."""

    repo = _Repo([row(net="80", gap=600, horizon=86400)] * 20)
    evaluator = EdgeEvaluator(repo=repo, min_samples=10, max_gap_minutes=30)

    decision = await evaluator.evaluate(
        signal_id="s3",
        symbol="BTCUSDT",
        features={"atr_5m_bps": 20.0},
        probability=0.7,
        horizon_seconds=86400,
        round_trip_cost_bps=Decimal("30"),
        now=NOW,
    )

    assert decision.reason == "insufficient_evidence"
