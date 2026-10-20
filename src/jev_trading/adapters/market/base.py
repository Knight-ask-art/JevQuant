"""Market-data provider protocol."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

from ...domain.market import Candle, MarketSnapshot, SymbolFilters


class MarketDataProvider(Protocol):
    source: str

    async def snapshots(
        self, symbols: Sequence[str], now: datetime | None = None
    ) -> list[MarketSnapshot]: ...

    async def klines(
        self, symbol: str, interval: str = "1m", limit: int = 120, warmup: int = 0
    ) -> list[Candle]: ...

    async def filters(self, symbol: str) -> SymbolFilters: ...

    async def health(self) -> dict[str, object]: ...

    async def close(self) -> None: ...
