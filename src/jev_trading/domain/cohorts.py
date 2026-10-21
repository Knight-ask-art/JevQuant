"""Small, pure readings of a snapshot used to group one trade with its peers.

Splitting thresholds are *fixed absolute bands*, not quantiles of whatever
history happens to exist. A quantile split makes a trade's cohort depend on
when it was evaluated -- today's "high volatility" would be yesterday's
"normal" -- which turns an out-of-sample comparison into the same trade
measured against itself. Fixed bands have the opposite flaw, a lopsided split,
and that flaw is visible in the sample counts instead of hidden.

Two dimensions are kept deliberately coarse. Every extra bucket divides the
sample, and the estimate this groups for is already the thing that needs the
most data.
"""

from __future__ import annotations

from typing import Any

#: 5-minute ATR in bps. Calibrated loosely for a 1m/5m crypto chart: below
#: 10 bps the round trip is usually the whole move and the regime gate says so.
VOLATILITY_BANDS_BPS = (10.0, 30.0)

#: Jev's choice probability. Untreated as *probability of profit* -- it is a
#: confidence score, and whether it means anything is the question the
#: calibration work will answer.
CONFIDENCE_BANDS = (0.6, 0.8)


def _band(value: float | None, thresholds: tuple[float, ...], names: tuple[str, ...]) -> str:
    if value is None:
        return "unknown"
    for threshold, name in zip(thresholds, names):
        if value < threshold:
            return name
    return names[-1]


def volatility_class(raw_atr_bps: Any) -> str:
    """``low`` / ``normal`` / ``high`` / ``unknown``.

    Zero and missing are both "unknown": the feature is 0.0 when it could not
    be computed, and a real 5-minute true range is never exactly zero.
    """

    if raw_atr_bps is None:
        return "unknown"
    value = float(raw_atr_bps)
    if value <= 0:
        return "unknown"
    return _band(value, VOLATILITY_BANDS_BPS, ("low", "normal", "high"))


def confidence_class(probability: Any) -> str:
    if probability is None:
        return "unknown"
    return _band(float(probability), CONFIDENCE_BANDS, ("low", "mid", "high"))


def symbol_class(symbol: str | None) -> str:
    return (symbol or "").upper() or "unknown"
