"""Market-data provider protocol."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

from ...domain.market import MarketSnapshot, SymbolFilters


class MarketDataProvider(Protocol):
    source: str

    async def snapshots(
        self, symbols: Sequence[str], now: datetime | None = None
    ) -> list[MarketSnapshot]: ...

    async def filters(self, symbol: str) -> SymbolFilters: ...

    async def health(self) -> dict[str, object]: ...

    async def close(self) -> None: ...
