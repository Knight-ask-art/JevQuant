"""Deterministic synthetic market used for local paper demos.

This provider is deliberately loud: every snapshot carries
``is_synthetic=True`` and source ``synthetic``.  It must never be enabled
in production (the settings validator rejects that combination).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Sequence

from ...domain.market import Candle, MarketSnapshot, SymbolFilters
from ...services.features import build_features
from ...services.klines import SYNTHETIC_LOOKBACK_MINUTES, upsample


def extend_history(
    candles: list[Candle], minutes: int, *, seed: int, symbol: str
) -> list[Candle]:
    """Prepend a plausible past so longer chart intervals have enough bars.

    Deterministic per symbol, mean-reverting around the base price. It exists
    only to make the demo chart legible and is labelled synthetic end to end.
    """

    if len(candles) >= minutes:
        return candles
    rng = random.Random(f"{seed}:{symbol}:extended")
    base = float(BASE_PRICES.get(symbol, Decimal("100")))
    needed = minutes - len(candles)
    start = candles[0].open_time - timedelta(minutes=needed)
    price = candles[0].open
    extended: list[Candle] = []
    for index in range(needed):
        mean_reversion = (base - price) / base * 0.002
        price = max(0.01, price * (1.0 + mean_reversion + rng.gauss(0.0, 0.0009)))
        open_price = extended[-1].close if extended else price
        close_time = start + timedelta(minutes=index + 1)
        extended.append(
            Candle(
                open_time=close_time - timedelta(minutes=1),
                close_time=close_time,
                open=open_price,
                high=max(open_price, price) * (1.0 + abs(rng.gauss(0, 0.0004))),
                low=min(open_price, price) * (1.0 - abs(rng.gauss(0, 0.0004))),
                close=price,
                volume=max(1.0, rng.gauss(1000, 250)),
            )
        )
    return extended + candles

BASE_PRICES: dict[str, Decimal] = {
    "BTCUSDT": Decimal("68000"),
    "ETHUSDT": Decimal("3400"),
    "SOLUSDT": Decimal("170"),
}

FILTER_SPECS: dict[str, dict[str, str]] = {
    "BTCUSDT": {
        "min_qty": "0.00001",
        "max_qty": "9000",
        "step_size": "0.00001",
        "min_price": "0.01",
        "max_price": "1000000",
        "tick_size": "0.01",
        "min_notional": "5",
    },
    "ETHUSDT": {
        "min_qty": "0.0001",
        "max_qty": "90000",
        "step_size": "0.0001",
        "min_price": "0.01",
        "max_price": "100000",
        "tick_size": "0.01",
        "min_notional": "5",
    },
    "SOLUSDT": {
        "min_qty": "0.001",
        "max_qty": "900000",
        "step_size": "0.001",
        "min_price": "0.01",
        "max_price": "10000",
        "tick_size": "0.01",
        "min_notional": "5",
    },
}


class SyntheticMarketDataProvider:
    source = "synthetic"

    def __init__(
        self,
        seed: int = 42,
        symbols: Sequence[str] | None = None,
        *,
        advance_each_call: bool = False,
    ) -> None:
        self.seed = seed
        self.symbols = list(symbols or BASE_PRICES.keys())
        self._candles: dict[str, list[Candle]] = {}
        self._last_minute: dict[str, datetime] = {}
        self.advance_each_call = advance_each_call
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        self._sim_clock = now
        for symbol in self.symbols:
            self._seed_history(symbol, now)

    def _seed_history(self, symbol: str, now: datetime) -> None:
        rng = random.Random(f"{self.seed}:{symbol}:history")
        base = float(BASE_PRICES.get(symbol, Decimal("100")))
        prices: list[float] = [base]
        for index in range(1, 240):
            mean_reversion = (base - prices[-1]) / base * 0.02
            shock = rng.gauss(0.0, 0.0009)
            prices.append(max(0.01, prices[-1] * (1.0 + mean_reversion + shock)))
        candles: list[Candle] = []
        start = now - timedelta(minutes=240)
        for index, close in enumerate(prices):
            open_price = prices[index - 1] if index else close
            high = max(open_price, close) * (1.0 + abs(rng.gauss(0, 0.0004)))
            low = min(open_price, close) * (1.0 - abs(rng.gauss(0, 0.0004)))
            volume = max(1.0, rng.gauss(1000, 250))
            close_time = start + timedelta(minutes=index + 1)
            candles.append(
                Candle(
                    open_time=close_time - timedelta(minutes=1),
                    close_time=close_time,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                )
            )
        self._candles[symbol] = candles
        self._last_minute[symbol] = candles[-1].close_time

    def _advance(self, symbol: str, now: datetime) -> None:
        current_minute = now.replace(second=0, microsecond=0)
        last = self._last_minute[symbol]
        while last < current_minute:
            index = len(self._candles[symbol])
            rng = random.Random(f"{self.seed}:{symbol}:{index}")
            previous = self._candles[symbol][-1]
            base = float(BASE_PRICES.get(symbol, Decimal("100")))
            mean_reversion = (base - previous.close) / base * 0.02
            shock = rng.gauss(0.0, 0.0009)
            close = max(0.01, previous.close * (1.0 + mean_reversion + shock))
            high = max(previous.close, close) * (1.0 + abs(rng.gauss(0, 0.0004)))
            low = min(previous.close, close) * (1.0 - abs(rng.gauss(0, 0.0004)))
            self._candles[symbol].append(
                Candle(
                    open_time=last,
                    close_time=last + timedelta(minutes=1),
                    open=previous.close,
                    high=high,
                    low=low,
                    close=close,
                    volume=max(1.0, rng.gauss(1000, 250)),
                )
            )
            last += timedelta(minutes=1)
            self._candles[symbol] = self._candles[symbol][-400:]
        self._last_minute[symbol] = last

    async def snapshots(
        self, symbols: Sequence[str], now: datetime | None = None
    ) -> list[MarketSnapshot]:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        if self.advance_each_call:
            # Demo clock: every cycle is a fresh closed candle. as_of moves
            # with the simulated clock so repeated cycles are distinct while
            # the data remains internally consistent.
            self._sim_clock = self._sim_clock + timedelta(minutes=1)
            now = self._sim_clock
        snapshots: list[MarketSnapshot] = []
        for symbol in symbols:
            if symbol not in self._candles:
                self._seed_history(symbol, now)
            self._advance(symbol, now)
            candles = self._candles[symbol][-240:]
            last = candles[-1]
            minute_key = int(now.timestamp()) // 60
            rng = random.Random(f"{self.seed}:{symbol}:{minute_key}")
            spread_bps = Decimal(str(round(rng.uniform(1.0, 4.0), 4)))
            mid = Decimal(str(round(last.close, 2)))
            half = mid * spread_bps / Decimal("20000")
            bid = (mid - half).quantize(Decimal("0.01"))
            ask = (mid + half).quantize(Decimal("0.01"))
            if ask <= bid:
                ask = bid + Decimal("0.01")
            imbalance = rng.uniform(-0.35, 0.35)
            features = build_features(candles, book_imbalance=imbalance)
            snapshots.append(
                MarketSnapshot(
                    symbol=symbol,
                    as_of=now,
                    source=self.source,
                    bid=bid,
                    ask=ask,
                    candle_close_at=last.close_time,
                    features=features,
                    is_synthetic=True,
                )
            )
        return snapshots

    async def filters(self, symbol: str) -> SymbolFilters:
        spec = FILTER_SPECS.get(symbol)
        if spec is None:
            raise ValueError(f"no synthetic filters configured for {symbol}")
        return SymbolFilters(
            symbol=symbol,
            status="TRADING",
            base_asset=symbol[:-4],
            quote_asset="USDT",
            min_qty=Decimal(spec["min_qty"]),
            max_qty=Decimal(spec["max_qty"]),
            step_size=Decimal(spec["step_size"]),
            min_price=Decimal(spec["min_price"]),
            max_price=Decimal(spec["max_price"]),
            tick_size=Decimal(spec["tick_size"]),
            min_notional=Decimal(spec["min_notional"]),
            notional_apply_to_market=True,
        )

    async def klines(
        self, symbol: str, interval: str = "1m", limit: int = 120, warmup: int = 0
    ) -> list[Candle]:
        """Replay the simulated minute history, upsampled to ``interval``."""

        looked = SYNTHETIC_LOOKBACK_MINUTES.get(interval)
        if looked is None:
            raise ValueError(f"unsupported kline interval {interval!r}")
        lookback = max(looked, int(limit) + max(0, int(warmup)))
        now = (self._sim_clock if self.advance_each_call else datetime.now(UTC)).astimezone(
            UTC
        )
        if symbol not in self._candles:
            # Seeding uses the wall clock, so run it against wall time and let
            # _advance catch the history up to the simulated clock.
            self._seed_history(symbol, datetime.now(UTC).astimezone(UTC))
        self._advance(symbol, now)
        candles = extend_history(
            self._candles[symbol], lookback, seed=self.seed, symbol=symbol
        )
        self._candles[symbol] = candles
        series = upsample(candles[-lookback:], interval, now)
        return series[-int(limit) :]

    async def health(self) -> dict[str, object]:
        return {
            "healthy": True,
            "source": self.source,
            "synthetic": True,
            "note": "Deterministic demo feed - not real market data.",
        }

    async def close(self) -> None:
        return None
