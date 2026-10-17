"""Market data orchestration with a dynamic exchange-filter cache."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Sequence

from ..adapters.market.base import MarketDataProvider
from ..domain.market import MarketSnapshot, SymbolFilters


class MarketService:
    def __init__(self, provider: MarketDataProvider, *, filter_ttl_seconds: int = 600) -> None:
        self.provider = provider
        self.filter_ttl_seconds = filter_ttl_seconds
        self._filters: dict[str, tuple[float, SymbolFilters]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def snapshots(
        self, symbols: Sequence[str], now: datetime | None = None
    ) -> list[MarketSnapshot]:
        return await self.provider.snapshots(symbols, now=now or datetime.now(UTC))

    async def filters(self, symbol: str, *, force: bool = False) -> SymbolFilters:
        now = time.monotonic()
        cached = self._filters.get(symbol)
        if cached is not None and not force and now - cached[0] < self.filter_ttl_seconds:
            return cached[1]
        lock = self._locks.setdefault(symbol, asyncio.Lock())
        async with lock:
            cached = self._filters.get(symbol)
            if cached is not None and not force and time.monotonic() - cached[0] < self.filter_ttl_seconds:
                return cached[1]
            filters = await self.provider.filters(symbol)
            self._filters[symbol] = (time.monotonic(), filters)
            return filters

    async def health(self) -> dict[str, object]:
        return await self.provider.health()

    @property
    def source(self) -> str:
        return self.provider.source

    async def close(self) -> None:
        await self.provider.close()
