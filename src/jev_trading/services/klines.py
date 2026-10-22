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
from typing import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..domain.market import Candle
from .features import aggregate, closed_candles, rsi

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


def normalize_minute_candles(candles: Sequence[Candle]) -> list[Candle]:
    """Sort and deduplicate 1m OHLC bars without guessing conflicting prices.

    A provider may return out-of-order or repeated bars. A conflicting bar
    for the same open minute changes the stop/target ordering and must be
    rejected, not silently resolved in favour of a profitable replay.
    """

    by_open: dict[datetime, Candle] = {}
    for candle in candles:
        opening = candle.open_time
        closing = candle.close_time
        if (
            opening.tzinfo is None
            or closing.tzinfo is None
            or opening.utcoffset() is None
            or closing.utcoffset() is None
        ):
            raise ValueError("minute candles require timezone-aware timestamps")
        opening = opening.astimezone(UTC)
        if opening.second or opening.microsecond:
            raise ValueError("minute candle open time is not minute-aligned")
        close_utc = closing.astimezone(UTC)
        if not opening < close_utc <= opening + timedelta(minutes=1):
            raise ValueError("minute candle close time exceeds its one-minute interval")
        old = by_open.get(opening)
        if old is not None and old != candle:
            raise ValueError(f"conflicting duplicate minute candle at {opening.isoformat()}")
        by_open[opening] = candle
    return [by_open[timestamp] for timestamp in sorted(by_open)]


async def fetch_minute_range(market, symbol: str, start: datetime, end: datetime) -> list[Candle]:
    """Every closed 1m bar in ``[start, end]``, paging around the 1000-bar cap.

    A provider returns at most 1000 bars per request, so a window longer than
    that has to be walked forward. The loop stops when the data runs out, when
    a page makes no progress, or when it reaches ``end`` -- never because a
    page happened to be short, which is also what a gap at the cursor looks
    like.
    """

    bars: list[Candle] = []
    cursor = start
    while cursor < end:
        batch = await market.klines(symbol, interval="1m", limit=BINANCE_MAX_BARS, start=cursor)
        usable = normalize_minute_candles([
            bar
            for bar in closed_candles(batch, end)
            if bar.open_time >= start and bar.close_time > cursor
        ])
        if not usable:
            break
        bars = normalize_minute_candles([*bars, *usable])
        if usable[-1].close_time >= end:
            break
        cursor = usable[-1].close_time
    return bars


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


class IndicatorPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    i: int  # index into ``candles``; absent values are simply omitted
    v: float


class CandleSeries(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    interval: str
    source: str
    synthetic: bool
    candles: list[CandleOut]
    # name -> series, e.g. "sma20", "rsi14". Computed over the full fetched
    # history so the first visible bar already has a value, then trimmed to
    # the returned window by index.
    indicators: dict[str, list[IndicatorPoint]] = Field(default_factory=dict)


INDICATOR_NAMES = ("sma20", "sma60", "rsi14")


def compute_indicators(
    candles: Sequence[Candle], names: Sequence[str]
) -> dict[str, list[float | None]]:
    """Indicator values aligned to ``candles``; ``None`` where undefined.

    Every value at index ``i`` uses only candles ``0..i``: no lookahead, and
    nothing for a bar that has not closed yet.
    """

    closes = [candle.close for candle in candles]
    series: dict[str, list[float | None]] = {}
    for name in names:
        if name not in INDICATOR_NAMES:
            raise ValueError(f"unsupported indicator {name!r}")
        values: list[float | None] = []
        for index in range(len(candles)):
            if name.startswith("sma"):
                period = int(name[3:])
                window = closes[max(0, index - period + 1) : index + 1]
                values.append(sum(window) / period if len(window) == period else None)
            elif name == "rsi14":
                values.append(rsi(closes[: index + 1], 14))
        series[name] = values
    return series


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
    *,
    symbol: str,
    interval: str,
    source: str,
    synthetic: bool,
    candles: Sequence[Candle],
    indicators: Mapping[str, Sequence[float | None]] | None = None,
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
        indicators={
            name: [
                IndicatorPoint(i=index, v=round(value, 6))
                for index, value in enumerate(values)
                if value is not None
            ]
            for name, values in (indicators or {}).items()
        },
    )
