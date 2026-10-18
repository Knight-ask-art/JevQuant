"""Market data orchestration with a dynamic exchange-filter cache."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Sequence

from ..adapters.market.base import MarketDataProvider
from ..domain.market import MarketSnapshot, SymbolFilters


class MarketService:
    """Market snapshots plus an exchange-filter cache.

    The filter cache is bounded by ``filter_ttl_seconds``. Because a stale
    ``TRADING`` status could let an entry through while the venue has halted
    the symbol, entry paths call :meth:`entry_filters`: the cached value is
    re-used only while younger than ``entry_filter_ttl_seconds``, otherwise the
    exchange is re-queried (and a failure propagates, failing the entry closed).
    """

    ENTRY_FILTER_TTL_SECONDS = 60

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        filter_ttl_seconds: int = 600,
        entry_filter_ttl_seconds: int = ENTRY_FILTER_TTL_SECONDS,
    ) -> None:
        self.provider = provider
        self.filter_ttl_seconds = filter_ttl_seconds
        self.entry_filter_ttl_seconds = entry_filter_ttl_seconds
        self._filters: dict[str, tuple[float, SymbolFilters]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def snapshots(
        self, symbols: Sequence[str], now: datetime | None = None
    ) -> list[MarketSnapshot]:
        return await self.provider.snapshots(symbols, now=now or datetime.now(UTC))

    async def klines(self, symbol: str, interval: str = "1m", limit: int = 120):
        return await self.provider.klines(symbol, interval=interval, limit=limit)

    async def spot_candidates(self, limit: int = 40) -> list[dict[str, object]]:
        """Liquid USDT pairs the venue lists, when the provider can supply them."""

        candidates = getattr(self.provider, "spot_candidates", None)
        if candidates is None:
            return []
        return await candidates(limit=limit)

    def cached_filters(self, symbol: str) -> SymbolFilters | None:
        """The cached filters, only if they are fresh enough to trust for entries.

        Returns ``None`` for a cache miss or an entry-stale entry. Call
        :meth:`filters` to refresh; a provider failure must be propagated so the
        caller can fail closed.
        """

        cached = self._filters.get(symbol)
        if cached is None:
            return None
        if time.monotonic() - cached[0] >= self.entry_filter_ttl_seconds:
            return None
        return cached[1]

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

    async def entry_filters(self, symbol: str) -> SymbolFilters:
        """Filters safe for an entry: refreshed when the cache is older than the
        entry TTL. A provider error propagates (the entry must fail closed)."""

        cached = self.cached_filters(symbol)
        if cached is not None:
            return cached
        return await self.filters(symbol, force=True)

    async def health(self) -> dict[str, object]:
        return await self.provider.health()

    @property
    def source(self) -> str:
        return self.provider.source

    async def close(self) -> None:
        await self.provider.close()
