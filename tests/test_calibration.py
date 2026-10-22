"""Calibration: does the score mean what the gate assumes it means?

The failure this guards against is a monotone curve drawn through a handful of
points and then read as "0.8 means 80%". So the tests pin the refusals first:
too few rows is insufficient, a held-out check is required, and a score with no
discrimination is reported as such rather than dressed up.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from jev_trading.services.calibration import (
    CalibrationSample,
    apply_platt,
    calibrate,
    fit_platt,
    format_report,
    reliability,
)

START = datetime(2026, 10, 1, tzinfo=UTC)


def sample(confidence: float, won: bool, minutes: int = 0) -> CalibrationSample:
    return CalibrationSample(
        confidence=confidence, won=won, decided_at=START + timedelta(minutes=minutes)
    )


def overconfident(count: int) -> list[CalibrationSample]:
    """Scores clustered near 0.9 but only ~55% actually win."""

    rng = random.Random(5)
    out = []
    for index in range(count):
        confidence = min(0.99, max(0.6, rng.gauss(0.9, 0.06)))
        won = rng.random() < 0.55
        out.append(sample(confidence, won, index))
    return out


def honest(count: int) -> list[CalibrationSample]:
    """A score that is exactly the win rate: already calibrated."""

    rng = random.Random(9)
    out = []
    for index in range(count):
        confidence = min(0.95, max(0.55, rng.uniform(0.55, 0.95)))
        out.append(sample(confidence, rng.random() < confidence, index))
    return out


# -- refusals ----------------------------------------------------------------
def test_too_few_outcomes_is_insufficient_and_shows_what_is_missing():
    report = calibrate(overconfident(20), cohort="test/jev|exits-v1", min_samples=100)

    assert report["status"] == "insufficient"
    assert report["needed"] == 100
    assert report["have"] == 20
    assert "coefficients" not in report


def test_an_insufficient_report_still_says_what_the_sample_looks_like():
    report = calibrate(overconfident(20), cohort="c", min_samples=100)

    text = format_report(report)
    assert "description of the sample" in text
    assert "confidence bin" in text


def test_the_fit_uses_the_earliest_rows_and_checks_the_rest():
    """A fit that has not been checked out of sample is a description, not a calibration."""

    report = calibrate(overconfident(400), cohort="c", min_samples=100)

    assert report["status"] == "fitted"
    assert report["train_rows"] + report["held_out_rows"] == 400
    assert report["train_rows"] < report["held_out_rows"] * 3

    # The held-out rows are the latest ones, so moving them later must change
    # the check, not the fit.
    shifted = overconfident(400)
    shifted = [s for s in shifted[:280]] + [
        CalibrationSample(
            confidence=s.confidence, won=s.won, decided_at=s.decided_at + timedelta(days=30)
        )
        for s in shifted[280:]
    ]
    again = calibrate(shifted, cohort="c", min_samples=100)
    assert again["coefficients"] == report["coefficients"]


# -- what the fit says -------------------------------------------------------
def test_an_overconfident_model_gets_shrunk_towards_the_base_rate():
    report = calibrate(overconfident(1000), cohort="c", min_samples=100)
    coefficients = report["coefficients"]

    calibrated = apply_platt(coefficients, 0.9)

    assert calibrated < 0.9
    assert abs(calibrated - report["base_rate_train"]) < 0.15


def test_a_calibrated_model_is_left_roughly_alone():
    report = calibrate(honest(2000), cohort="c", min_samples=100)

    assert abs(apply_platt(report["coefficients"], 0.8) - 0.8) < 0.08


def test_a_score_with_no_discrimination_reports_a_cointoss_auc():
    """Every row scored the same: the score cannot tell winners from losers."""

    flat = [sample(0.7, index % 2 == 0, index) for index in range(200)]

    assert calibrate(flat, cohort="c")["auc"] == 0.5


def test_a_useful_score_shows_an_auc_above_half():
    report = calibrate(honest(2000), cohort="c")

    assert report["auc"] is not None
    assert report["auc"] > 0.55


def test_beating_the_base_rate_is_reported_explicitly():
    report = calibrate(overconfident(1000), cohort="c")

    assert report["brier_base_rate_held_out"] is not None
    assert report["beats_base_rate"] in (True, False)


# -- pieces ------------------------------------------------------------------
def test_platt_is_deterministic():
    samples = overconfident(300)
    assert fit_platt(samples) == fit_platt(samples)


def test_platt_never_leaves_the_unit_interval():
    coefficients = fit_platt(overconfident(300))
    for confidence in (0.0, 0.01, 0.5, 0.99, 1.0):
        assert 0.0 <= apply_platt(coefficients, confidence) <= 1.0


def test_reliability_hides_a_rate_a_bin_too_small_to_support():
    table = reliability([sample(0.85, True, 0), sample(0.85, False, 1)])

    assert table[0]["rows"] == 2
    assert table[0]["observed_win_rate"] is None


def test_the_cohort_name_separates_models_and_exit_schemas():
    """Two different questions must not share one mapping."""

    first = calibrate(overconfident(200), cohort="model-a|exits-v1")
    second = calibrate(overconfident(200), cohort="model-b|exits-v1")

    assert first["cohort"] != second["cohort"]
    assert first["rows"] == second["rows"] == 200


# -- cohorts and labels ------------------------------------------------------
from jev_trading.calibration import build_report, build_samples  # noqa: E402


class _Row:
    def __init__(self, **fields):
        self.__dict__.update(fields)


def row(model, confidence, net, *, minutes=60, gap=0, at=0):
    return _Row(
        model=model,
        confidence=confidence,
        horizon_seconds=minutes * 60,
        net_bps=net,
        gap_seconds=gap,
        decided_at=START + timedelta(minutes=at),
        symbol="BTCUSDT",
    )


def test_a_different_model_is_a_different_cohort():
    cohorts = build_samples(
        [row("model-a", 0.7, "10"), row("model-b", 0.7, "10")]
    )

    assert set(cohorts) == {"model-a|exit-60m", "model-b|exit-60m"}


def test_a_different_holding_window_is_a_different_cohort():
    """A 60-minute hold and a 24-hour hold are different bets."""

    cohorts = build_samples([row("m", 0.7, "10"), row("m", 0.7, "10", minutes=1440)])

    assert set(cohorts) == {"m|exit-60m", "m|exit-1440m"}


def test_a_window_never_seen_is_not_a_win_or_a_loss():
    """It has no label, so it cannot teach the calibration anything."""

    cohorts = build_samples([row("m", 0.7, None), row("m", 0.7, "10", gap=600)])

    assert sum(len(v) for v in cohorts.values()) == 0


def test_an_unknown_model_is_grouped_rather_than_dropped():
    cohorts = build_samples([row(None, 0.7, "10")])

    assert list(cohorts) == ["unknown-model|exit-60m"]


def test_the_report_keeps_every_cohort_and_says_when_each_is_insufficient():
    report = build_report(
        [row("a", 0.7, "10", at=0), row("b", 0.7, "-5", at=1)], min_samples=100
    )

    assert set(report["cohorts"]) == {"a|exit-60m", "b|exit-60m"}
    assert all(item["status"] == "insufficient" for item in report["cohorts"].values())
