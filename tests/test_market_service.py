"""Cached exchange filters must never admit an entry on a stale TRADING status."""

from __future__ import annotations

import pytest

from conftest import symbol_rules
from jev_trading.services.market_service import MarketService


class Provider:
    source = "test"

    def __init__(self, statuses: list[str]) -> None:
        self.statuses = statuses
        self.calls = 0

    async def filters(self, symbol: str):
        rules = symbol_rules(status=self.statuses[min(self.calls, len(self.statuses) - 1)])
        self.calls += 1
        return rules

    async def health(self):
        return {"healthy": True}

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_entry_filters_refresh_a_stale_cache():
    provider = Provider(["TRADING", "BREAK"])
    service = MarketService(provider, filter_ttl_seconds=600, entry_filter_ttl_seconds=0)
    await service.filters("BTCUSDT")  # populate the long-lived cache
    refreshed = await service.entry_filters("BTCUSDT")
    assert provider.calls == 2
    assert refreshed.status == "BREAK"


@pytest.mark.asyncio
async def test_entry_filters_reuse_a_fresh_cache():
    provider = Provider(["TRADING"])
    service = MarketService(provider, entry_filter_ttl_seconds=60)
    first = await service.entry_filters("BTCUSDT")
    second = await service.entry_filters("BTCUSDT")
    assert first.status == second.status == "TRADING"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_entry_filters_propagate_provider_failure():
    class Broken(Provider):
        async def filters(self, symbol):
            raise RuntimeError("exchangeInfo unavailable")

    service = MarketService(Broken([]), entry_filter_ttl_seconds=0)
    with pytest.raises(RuntimeError):
        await service.entry_filters("BTCUSDT")
