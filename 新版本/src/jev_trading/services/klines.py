"""Klines for charting.

The trading path only needs a snapshot: bid, ask and derived features from
the last closed candles. A chart needs the candles themselves, and those are
cheap to request with public endpoints.

Each provider exposes ``klines(symbol, interval, limit)``. The synthetic
provider replays its own minute history and upsamples; the Binance provider
asks ``/api/v3/klines``. Errors propagate as dependency failures so the UI
can report them honestly instead of drawing a fabricated line.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Sequence

from pydantic import BaseModel, ConfigDict

from ..domain.market import Candle
from .features import aggregate

INTERVALS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}

# Synthetic minute history kept in memory. Longer intervals need more raw
# minutes, so the provider replays this many before upsampling.
SYNTHETIC_LOOKBACK_MINUTES = {
    "1m": 240,
    "5m": 1500,
    "15m": 3000,
    "1h": 8400,
    "4h": 20000,
}

# Binance caps /api/v3/klines at 1000 bars per request.
BINANCE_MAX_BARS = 1000


class CandleOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    t: str  # candle open, ISO-8601 UTC
    o: float
    h: float
    l: float
    c: float
    v: float


class CandleSeries(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    interval: str
    source: str
    synthetic: bool
    candles: list[CandleOut]


def validate_interval(interval: str) -> str:
    if interval not in INTERVALS:
        raise ValueError(f"unsupported interval {interval!r}")
    return interval


def clamp_limit(limit: int) -> int:
    return max(20, min(int(limit), 500))


def upsample(candles: Sequence[Candle], interval: str, now: datetime) -> list[Candle]:
    """Aggregate 1m candles to ``interval``; only closed bars are returned."""

    minutes = int(INTERVALS[interval].total_seconds() // 60)
    closed = [candle for candle in candles if candle.close_time <= now]
    if minutes <= 1:
        return closed
    return aggregate(closed, minutes)


def to_series(
    *, symbol: str, interval: str, source: str, synthetic: bool, candles: Sequence[Candle]
) -> CandleSeries:
    return CandleSeries(
        symbol=symbol,
        interval=interval,
        source=source,
        synthetic=synthetic,
        candles=[
            CandleOut(
                t=candle.open_time.astimezone(UTC).isoformat(),
                o=candle.open,
                h=candle.high,
                l=candle.low,
                c=candle.close,
                v=candle.volume,
            )
            for candle in candles
        ],
    )
