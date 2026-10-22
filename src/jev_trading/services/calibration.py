"""Calibration of Jev's confidence: does 0.8 mean "wins 80% of the time"?

Jev's ``confidence`` is a score over its own choices. The system has been
reading it as ``entry_probability`` -- and the entry gate compares it against a
threshold. Both readings assume the score means something about outcomes, and
nobody has checked.

This module checks, and produces a mapping that can be checked again later:

- **Cohorts.** A calibration belongs to one (model, exit schema). A model that
  was re-tuned, or an exit geometry that changes what "win" means, needs its
  own mapping; pooling them would average two different questions.
- **Outcome first.** The recorded counterfactual return decides the label
  (``net_bps > 0``); the score is only the predictor. A counterfactual row
  now covers both rejected and Jev-eligible BUY decisions under identical
  replay assumptions; this is not a measurement of actual executed PnL.
- **Fitted on the past only.** Fitting takes the earliest part of a cohort and
  reports the rest as held out. A fit that has not been checked out of sample
  is a description, not a calibration.
- **Small samples are refused, not smoothed.** Below the minimum the answer is
  ``insufficient`` with the numbers that would be needed. A monotone fit drawn
  through eight points is a line with a story attached.

Two fitters are provided. ``platt`` (a logistic on the logit) is the default:
it has one shape and cannot memorise. ``isotonic`` is offered because it can
express non-monotone-adjacent behaviour, but it is deliberately *not* the
default -- with this little history it will happily fit the noise.

Nothing here changes a decision. It produces a versioned calibration document
and a report; applying it to the entry gate is a separate, explicit step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import exp, log
from typing import Any

from ..domain.money import ZERO, dec

#: Rows a cohort needs before a fit is reported as anything but insufficient.
DEFAULT_MIN_SAMPLES = 100
#: Fraction of the cohort used to fit; the rest is held out for the check.
DEFAULT_TRAIN_FRACTION = Decimal("0.7")
#: Bin edges for the reliability table.
RELIABILITY_BINS = (0.5, 0.6, 0.7, 0.8, 0.9, 1.01)
#: Below this many rows a bin's observed rate is not shown.
MIN_ROWS_PER_BIN = 5


@dataclass(frozen=True)
class CalibrationSample:
    """One recorded outcome and the score that preceded it."""

    confidence: float
    won: bool
    decided_at: datetime


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + exp(-x))
    z = exp(x)
    return z / (1.0 + z)


def fit_platt(samples: list[CalibrationSample], *, iterations: int = 200, lr: float = 0.1) -> dict[str, float]:
    """Fit ``p = sigmoid(a * logit(confidence) + b)`` by gradient descent.

    Deterministic: fixed start, fixed step count, no shuffling. The same
    sample always produces the same document, which matters because a
    calibration is versioned and downloaded into a decision path.
    """

    a, b = 1.0, 0.0
    rows = [(_logit(sample.confidence), 1.0 if sample.won else 0.0) for sample in samples]
    n = max(len(rows), 1)
    for _ in range(iterations):
        grad_a = 0.0
        grad_b = 0.0
        for x, label in rows:
            diff = _sigmoid(a * x + b) - label
            grad_a += diff * x
            grad_b += diff
        a -= lr * grad_a / n
        b -= lr * grad_b / n
    return {"a": round(a, 6), "b": round(b, 6)}


def apply_platt(coefficients: dict[str, float], confidence: float) -> float:
    return round(_sigmoid(coefficients["a"] * _logit(confidence) + coefficients["b"]), 6)


def _auc(samples: list[CalibrationSample]) -> float | None:
    """Probability that a winner was scored above a loser.

    Calibration without discrimination is a constant that happens to sit near
    the base rate: it can be "calibrated" and useless. The AUC says which.
    """

    wins = [sample.confidence for sample in samples if sample.won]
    losses = [sample.confidence for sample in samples if not sample.won]
    if not wins or not losses:
        return None
    total = 0.0
    for win in wins:
        for loss in losses:
            if win > loss:
                total += 1.0
            elif win == loss:
                total += 0.5
    return round(total / (len(wins) * len(losses)), 4)


def reliability(samples: list[CalibrationSample]) -> list[dict[str, Any]]:
    """Observed win rate per confidence bucket, next to the bin's mean score."""

    out: list[dict[str, Any]] = []
    for low, high in zip(RELIABILITY_BINS, RELIABILITY_BINS[1:]):
        rows = [s for s in samples if low <= s.confidence < high]
        if not rows:
            continue
        observed = sum(1 for s in rows if s.won) / len(rows)
        mean_score = sum(s.confidence for s in rows) / len(rows)
        out.append(
            {
                "bin": f"{low:.2f}-{min(high, 1.0):.2f}",
                "rows": len(rows),
                "mean_confidence": round(mean_score, 4),
                "observed_win_rate": round(observed, 4) if len(rows) >= MIN_ROWS_PER_BIN else None,
            }
        )
    return out


def brier(samples: list[CalibrationSample], predict: Any) -> float | None:
    if not samples:
        return None
    total = sum((predict(sample.confidence) - (1.0 if sample.won else 0.0)) ** 2 for sample in samples)
    return round(total / len(samples), 6)


def _split(
    samples: list[CalibrationSample], train_fraction: Decimal
) -> tuple[list[CalibrationSample], list[CalibrationSample]]:
    ordered = sorted(samples, key=lambda sample: sample.decided_at)
    cut = int(len(ordered) * float(train_fraction))
    return ordered[:cut], ordered[cut:]


def calibrate(
    samples: list[CalibrationSample],
    *,
    cohort: str,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    train_fraction: Decimal = DEFAULT_TRAIN_FRACTION,
    fitted_at: datetime | None = None,
) -> dict[str, Any]:
    """Fit on the early part of a cohort, report the held-out check.

    The returned document is what would be versioned; ``status`` says whether
    it is allowed to be used.
    """

    base: dict[str, Any] = {
        "cohort": cohort,
        "fitted_at": (fitted_at.isoformat() if fitted_at else None),
        "estimator": "platt-logit-v1",
        "rows": len(samples),
    }
    if len(samples) < min_samples:
        return {
            **base,
            "status": "insufficient",
            "needed": min_samples,
            "have": len(samples),
            "reliability": reliability(samples),
            "note": (
                "Not enough outcomes to fit a calibration. The table below is a "
                "description of the sample, not a calibration."
            ),
        }

    train, held_out = _split(samples, train_fraction)
    if not train or not held_out:
        return {**base, "status": "insufficient", "have": len(samples), "needed": min_samples}

    coefficients = fit_platt(train)
    predict = lambda confidence: apply_platt(coefficients, confidence)  # noqa: E731
    train_won = sum(1 for s in train if s.won) / len(train)
    held_rate = sum(1 for s in held_out if s.won) / len(held_out)
    # A calibration is only worth having if it beats "just say the base rate".
    held_brier = brier(held_out, predict)
    base_brier = brier(held_out, lambda _conf: train_won)
    return {
        **base,
        "status": "fitted",
        "coefficients": coefficients,
        "train_rows": len(train),
        "held_out_rows": len(held_out),
        "base_rate_train": round(train_won, 4),
        "base_rate_held_out": round(held_rate, 4),
        "brier_held_out": held_brier,
        "brier_base_rate_held_out": base_brier,
        "beats_base_rate": (
            held_brier is not None and base_brier is not None and held_brier < base_brier
        ),
        "auc": _auc(samples),
        "auc_held_out": _auc(held_out),
        "reliability": reliability(samples),
    }


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"Cohort: {report['cohort']}",
        f"Rows: {report['rows']}  Status: {report['status']}",
    ]
    if report["status"] == "insufficient":
        lines.append(
            f"Need {report.get('needed')} outcomes to fit; have {report.get('have')}. "
            "The table below is a description of the sample, not a calibration."
        )
    else:
        coefficients = report["coefficients"]
        lines.append(
            f"Platt: p = sigmoid({coefficients['a']} * logit(confidence) + {coefficients['b']})"
        )
        lines.append(
            f"Held out: {report['held_out_rows']} rows, win rate "
            f"{report['base_rate_held_out']}, Brier {report['brier_held_out']} "
            f"vs base-rate {report['brier_base_rate_held_out']} "
            f"({'better' if report['beats_base_rate'] else 'NOT better'})"
        )
        lines.append(
            f"AUC: {report['auc']} overall, {report['auc_held_out']} held out"
        )
    lines.append("")
    lines.append("| confidence bin | rows | mean confidence | observed win rate |")
    lines.append("| --- | --- | --- | --- |")
    for row in report["reliability"]:
        lines.append(
            f"| {row['bin']} | {row['rows']} | {row['mean_confidence']} | "
            f"{'-' if row['observed_win_rate'] is None else row['observed_win_rate']} |"
        )
    return "\n".join(lines)


def label_from_outcomes(rows: list[Any]) -> list[Any]:
    """The rows that can carry a win/loss label: decided, fully-seen windows."""

    out = []
    for row in rows:
        if getattr(row, "net_bps", None) is None:
            continue
        if getattr(row, "gap_seconds", 0):
            continue
        out.append(row)
    return out


def is_win(row: Any) -> bool:
    return dec(row.net_bps) > ZERO
