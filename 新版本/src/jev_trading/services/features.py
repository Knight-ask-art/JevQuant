"""Deterministic, lookahead-free feature engineering.

Only *closed* candles are accepted.  Indicator math is float-based because
these are non-economic model inputs; any price used for sizing remains
Decimal at the execution boundary.
"""

from __future__ import annotations

from datetime import datetime
from math import sqrt
from typing import Sequence

# The candle shape lives in the domain so market providers can return it
# without importing a service. Re-exported here for existing callers.
from ..domain.market import Candle

__all__ = ["Candle"]


def sma(values: Sequence[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    window = values[-period:]
    return sum(window) / period


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    """Wilder's RSI. Requires at least period+1 values."""
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: Sequence[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    true_ranges: list[float] = []
    for index in range(1, len(candles)):
        current = candles[index]
        previous = candles[index - 1]
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    window = true_ranges[-period:]
    return sum(window) / period


def zscore_last(values: Sequence[float], period: int) -> float | None:
    if len(values) < period or period < 2:
        return None
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((value - mean) ** 2 for value in window) / (period - 1)
    if variance <= 0:
        return 0.0
    return (window[-1] - mean) / sqrt(variance)


def vwap(candles: Sequence[Candle], period: int) -> float | None:
    if len(candles) < period:
        return None
    window = candles[-period:]
    total_pv = 0.0
    total_volume = 0.0
    for candle in window:
        typical = (candle.high + candle.low + candle.close) / 3.0
        total_pv += typical * candle.volume
        total_volume += candle.volume
    if total_volume <= 0:
        return None
    return total_pv / total_volume


def aggregate(candles: Sequence[Candle], minutes: int) -> list[Candle]:
    """Aggregate 1m candles into N-minute candles by close-time bucket."""
    if minutes <= 1:
        return list(candles)
    buckets: dict[int, list[Candle]] = {}
    order: list[int] = []
    for candle in candles:
        key = int(candle.close_time.timestamp()) // (minutes * 60)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(candle)
    aggregated: list[Candle] = []
    for key in order:
        group = buckets[key]
        if len(group) < minutes:
            continue
        aggregated.append(
            Candle(
                open_time=group[0].open_time,
                close_time=group[-1].close_time,
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
            )
        )
    return aggregated


def build_features(
    candles_1m: Sequence[Candle], *, book_imbalance: float = 0.0
) -> dict[str, float]:
    if len(candles_1m) < 30:
        raise ValueError("at least 30 closed 1m candles are required")
    closes = [c.close for c in candles_1m]
    candles_5m = aggregate(candles_1m, 5)
    closes_5m = [c.close for c in candles_5m]
    volumes_5m = [c.volume for c in candles_5m]
    last = closes[-1]

    def basis_points(numerator: float) -> float:
        if last <= 0:
            return 0.0
        return numerator / last * 10_000.0

    return_1m = 0.0
    if len(closes) >= 2 and closes[-2] > 0:
        return_1m = (last / closes[-2] - 1.0) * 10_000.0
    return_5m = 0.0
    if len(closes) >= 6 and closes[-6] > 0:
        return_5m = (last / closes[-6] - 1.0) * 10_000.0
    return_15m = 0.0
    if len(closes) >= 16 and closes[-16] > 0:
        return_15m = (last / closes[-16] - 1.0) * 10_000.0

    rsi_value = rsi(closes_5m, 14)
    atr_value = atr(candles_5m, 14)
    vwap_value = vwap(candles_5m, 24)
    volume_z = zscore_last(volumes_5m, 20)

    features = {
        "return_1m_bps": return_1m,
        "return_5m_bps": return_5m,
        "return_15m_bps": return_15m,
        "rsi_5m": rsi_value if rsi_value is not None else 50.0,
        "atr_5m_bps": (atr_value / last * 10_000.0) if atr_value and last > 0 else 0.0,
        "orderbook_imbalance_10": float(max(-1.0, min(1.0, book_imbalance))),
        "volume_z_5m": volume_z if volume_z is not None else 0.0,
        "distance_vwap_5m_bps": basis_points(last - vwap_value) if vwap_value else 0.0,
        "sma20_1m_bps": (
            basis_points(last - sma(closes, 20)) if sma(closes, 20) else 0.0
        ),
        "sma60_1m_bps": (
            basis_points(last - sma(closes, 60)) if sma(closes, 60) else 0.0
        ),
        "range_1h_bps": (
            (max(c.high for c in candles_1m[-60:]) - min(c.low for c in candles_1m[-60:]))
            / last
            * 10_000.0
            if last > 0
            else 0.0
        ),
    }
    return {key: float(value) for key, value in features.items()}


def closed_candles(candles: Sequence[Candle], now: datetime) -> list[Candle]:
    return [candle for candle in candles if candle.close_time <= now]
