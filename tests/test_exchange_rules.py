from decimal import Decimal
from unittest.mock import MagicMock
import pytest
from jevquant.exchange import BinancePublicAdapter, BinanceTestnetAdapter


def make_mock_exchange(status="TRADING", filters=None, ticker_last="65000.50"):
    mock_ex = MagicMock()
    if filters is None:
        filters = [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000.00", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000.00", "stepSize": "0.00001"},
            {"filterType": "NOTIONAL", "minNotional": "5.00000000", "applyMinToMarket": True},
        ]

    mock_ex.load_markets.return_value = {
        "BTC/USDT": {
            "info": {
                "status": status,
                "filters": filters
            }
        }
    }
    mock_ex.fetch_ticker.return_value = {"last": ticker_last}
    return mock_ex


def test_market_rules_parsing_with_decimal_precision():
    mock_ex = make_mock_exchange()
    adapter = BinancePublicAdapter(exchange=mock_ex)

    rules = adapter.fetch_market_rules("BTC/USDT")
    assert rules["symbol"] == "BTC/USDT"
    assert rules["status"] == "TRADING"
    assert isinstance(rules["min_notional"], Decimal)
    assert rules["min_notional"] == Decimal("5.00000000")
    assert isinstance(rules["step_size"], Decimal)
    assert rules["step_size"] == Decimal("0.00001")
    assert isinstance(rules["tick_size"], Decimal)
    assert rules["tick_size"] == Decimal("0.01")


def test_fail_closed_when_symbol_not_trading():
    mock_ex = make_mock_exchange(status="BREAK")
    adapter = BinancePublicAdapter(exchange=mock_ex)

    with pytest.raises(RuntimeError, match="not in TRADING status"):
        adapter.fetch_market_rules("BTC/USDT")


def test_fail_closed_when_critical_filters_missing():
    # Missing NOTIONAL / MIN_NOTIONAL filter
    filters = [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000.00", "tickSize": "0.01"},
        {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000.00", "stepSize": "0.00001"},
    ]
    mock_ex = make_mock_exchange(filters=filters)
    adapter = BinancePublicAdapter(exchange=mock_ex)

    with pytest.raises(ValueError, match="Critical filter MIN_NOTIONAL missing"):
        adapter.fetch_market_rules("BTC/USDT")


def test_strictly_only_closed_candles_returned():
    mock_ex = MagicMock()
    # 3 candles; the 3rd is the ongoing, unclosed candle
    mock_ex.fetch_ohlcv.return_value = [
        [1700000000000, 60000.0, 60500.0, 59900.0, 60200.0, 100.5],
        [1700003600000, 60200.0, 60800.0, 60100.0, 60700.0, 120.3],
        [1700007200000, 60700.0, 61000.0, 60600.0, 60900.0, 50.0],  # Active candle
    ]
    adapter = BinancePublicAdapter(exchange=mock_ex)

    closed_candles = adapter.fetch_closed_candles("BTC/USDT", "1h", limit=2)
    # Must only return the first 2 closed candles
    assert len(closed_candles) == 2
    assert closed_candles[0]["close"] == Decimal("60200.0")
    assert closed_candles[1]["close"] == Decimal("60700.0")
    assert isinstance(closed_candles[0]["open"], Decimal)


def test_testnet_market_rules_use_exact_exchange_filters():
    mock_ex = make_mock_exchange()
    adapter = BinanceTestnetAdapter(api_key="test", api_secret="test", exchange=mock_ex)
    rules = adapter.fetch_market_rules("BTC/USDT")
    assert rules["min_notional"] == Decimal("5.00000000")
    assert rules["min_qty"] == Decimal("0.00001")
    assert rules["status"] == "TRADING"
    mock_ex.load_markets.assert_called_once_with(reload=True)


@pytest.mark.parametrize("filters", [
    [],
    [
        {"filterType": "LOT_SIZE", "minQty": "0.00001",
         "maxQty": "9000.00", "stepSize": "0.00001"},
        {"filterType": "PRICE_FILTER", "minPrice": "0.01",
         "maxPrice": "1000000.00", "tickSize": "0.01"},
    ],
])
def test_testnet_market_rules_fail_closed_on_missing_filters(filters):
    adapter = BinanceTestnetAdapter(
        api_key="test", api_secret="test",
        exchange=make_mock_exchange(filters=filters),
    )
    with pytest.raises((RuntimeError, ValueError), match="filter"):
        adapter.fetch_market_rules("BTC/USDT")


@pytest.mark.parametrize("status", ["BREAK", None])
def test_testnet_market_rules_fail_closed_on_untradable_or_missing_status(status):
    adapter = BinanceTestnetAdapter(
        api_key="test", api_secret="test",
        exchange=make_mock_exchange(status=status),
    )
    with pytest.raises(RuntimeError, match="not in TRADING status"):
        adapter.fetch_market_rules("BTC/USDT")
