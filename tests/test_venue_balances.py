"""Venue balance parsing for the testnet and Hummingbot adapters."""

from __future__ import annotations

from decimal import Decimal

import pytest

from jev_trading.adapters.execution.binance_testnet import BinanceTestnetExecutionAdapter
from jev_trading.adapters.execution.hummingbot import HummingbotExecutionAdapter
from jev_trading.domain.errors import DependencyBadResponseError
from jev_trading.storage.db import Database
from jev_trading.storage.repository import Repository


@pytest.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'venue.db'}")
    await db.create_all()
    try:
        yield Repository(db)
    finally:
        await db.dispose()


def make_testnet_adapter(repo, payload):
    """A testnet adapter whose signed request returns ``payload``."""

    adapter = BinanceTestnetExecutionAdapter(
        repo, base_url="https://testnet.invalid", api_key="k", api_secret="s"
    )

    async def _request(method, path, params=None):
        assert method == "GET" and path == "/api/v3/account"
        return payload

    adapter._request = _request  # type: ignore[method-assign]
    return adapter


async def test_testnet_balances_map_quote_and_base_assets(repo):
    adapter = make_testnet_adapter(
        repo,
        {
            "accountType": "SPOT",
            "balances": [
                {"asset": "USDT", "free": "1000.25", "locked": "12.5"},
                {"asset": "BTC", "free": "0.001", "locked": "0"},
                {"asset": "ETH", "free": "0", "locked": "0"},
            ],
        },
    )
    view = await adapter.fetch_balances()
    assert view is not None
    assert view.route == "testnet"
    assert view.free_quote == Decimal("1000.25")
    assert view.locked_quote == Decimal("12.5")
    assert view.total_quote == Decimal("1012.75")
    # Zero balances are dropped; base tolerance is exact without exchangeInfo.
    assert view.base_quantity == {"BTC": Decimal("0.001")}
    assert view.base_tolerance["BTC"] == Decimal("0")
    assert view.is_simulated is False
    assert "USDT" not in view.base_quantity


async def test_testnet_unconfigured_returns_none(repo):
    adapter = BinanceTestnetExecutionAdapter(
        repo, base_url="https://testnet.invalid", api_key=None, api_secret=None
    )
    assert await adapter.fetch_balances() is None


async def test_testnet_malformed_payload_is_refused(repo):
    adapter = make_testnet_adapter(repo, {"balances": [{"asset": "USDT", "free": "lots"}]})
    with pytest.raises(DependencyBadResponseError):
        await adapter.fetch_balances()


def hummingbot_adapter(repo, payload, status=200):
    adapter = HummingbotExecutionAdapter(
        repo, base_url="https://hb.invalid", api_key="k", connector_name="binance"
    )

    class Response:
        status_code = status

        def json(self):
            return payload

    class Client:
        async def post(self, url, headers=None, json=None):
            return Response()

    adapter._client = Client()  # type: ignore[assignment]
    return adapter


async def test_hummingbot_balances_map_pairs(repo):
    adapter = hummingbot_adapter(
        repo,
        {
            "balances": {
                "binance": {
                    "BTC-USDT": {
                        "base_available": 0.001,
                        "base_total": 0.001,
                        "quote_available": 900.0,
                        "quote_total": 950.0,
                    },
                    "ETH-USDT": {
                        "base_available": 0.1,
                        "base_total": 0.1,
                        "quote_available": 50.0,
                        "quote_total": 50.0,
                    },
                }
            }
        },
    )
    view = await adapter.fetch_balances()
    assert view is not None
    assert view.route == "live"
    assert view.free_quote == Decimal("950")
    assert view.locked_quote == Decimal("50")
    assert view.base_quantity == {"BTC": Decimal("0.001"), "ETH": Decimal("0.1")}
    assert view.is_simulated is False


async def test_hummingbot_missing_connector_is_refused(repo):
    adapter = hummingbot_adapter(repo, {"balances": {"kucoin": {}}})
    with pytest.raises(DependencyBadResponseError):
        await adapter.fetch_balances()


async def test_hummingbot_http_error_is_refused(repo):
    adapter = hummingbot_adapter(repo, {}, status=503)
    with pytest.raises(DependencyBadResponseError):
        await adapter.fetch_balances()
