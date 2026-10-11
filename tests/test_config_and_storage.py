from decimal import Decimal
from pathlib import Path
import pytest
from jevquant.config import AppConfig, load_config_from_env
from jevquant.storage import Storage


def test_live_trading_safety_gates():
    # Attempting live mode without enable_live_trading must fail
    cfg = AppConfig(trading_mode="live", enable_live_trading=False)
    with pytest.raises(PermissionError, match="ENABLE_LIVE_TRADING is false"):
        cfg.validate_safety_gates(cli_confirm_live=True)

    # Attempting live mode without CLI --confirm-live flag must fail
    cfg2 = AppConfig(trading_mode="live", enable_live_trading=True, binance_api_key="k", binance_api_secret="s", live_initial_capital_usdt=Decimal("7.00"))
    with pytest.raises(PermissionError, match="CLI --confirm-live"):
        cfg2.validate_safety_gates(cli_confirm_live=False)

    # Exceeding budget safety cap (> 10 USDT, ~70 CNY) must fail
    cfg3 = AppConfig(trading_mode="live", enable_live_trading=True, binance_api_key="k", binance_api_secret="s", live_initial_capital_usdt=Decimal("50.00"))
    with pytest.raises(ValueError, match="exceeds safety cap"):
        cfg3.validate_safety_gates(cli_confirm_live=True)


def test_storage_rules_snapshot_roundtrip(tmp_path: Path):
    db_file = tmp_path / "test.db"
    store = Storage(db_file)

    sample_rules = {
        "symbol": "BTC/USDT",
        "status": "TRADING",
        "min_notional": Decimal("5.00000000"),
        "min_qty": Decimal("0.00001"),
        "max_qty": Decimal("9000.00"),
        "step_size": Decimal("0.00001"),
        "min_price": Decimal("0.01"),
        "max_price": Decimal("1000000.00"),
        "tick_size": Decimal("0.01"),
        "raw_filters": [{"filterType": "PRICE_FILTER"}],
        "captured_at_utc": "2026-10-11T00:00:00Z"
    }

    snap_id = store.save_market_rules_snapshot(sample_rules)
    assert snap_id > 0

    retrieved = store.get_latest_market_rules("BTC/USDT")
    assert retrieved is not None
    assert retrieved["min_notional"] == Decimal("5.00000000")
    assert retrieved["step_size"] == Decimal("0.00001")
    assert isinstance(retrieved["min_notional"], Decimal)
    assert retrieved["status"] == "TRADING"


def test_storage_candles_roundtrip(tmp_path: Path):
    db_file = tmp_path / "test.db"
    store = Storage(db_file)

    candle = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "candle_close_utc": "2026-10-11T01:00:00Z",
        "open": Decimal("65000.00"),
        "high": Decimal("65500.00"),
        "low": Decimal("64900.00"),
        "close": Decimal("65300.00"),
        "volume": Decimal("125.4"),
    }
    store.save_candle(candle)

    candles = store.get_candles("BTC/USDT", "1h")
    assert len(candles) == 1
    assert candles[0]["close"] == Decimal("65300.00")
    assert isinstance(candles[0]["volume"], Decimal)


def test_get_candles_returns_latest_window_in_chronological_order(tmp_path: Path):
    store = Storage(tmp_path / "latest_window.db")
    for hour in range(1, 7):
        store.save_candle({
            "symbol": "BTC/USDT", "timeframe": "1h",
            "candle_close_utc": f"2026-10-11T{hour:02d}:00:00+00:00",
            "open": Decimal(str(hour)), "high": Decimal(str(hour)),
            "low": Decimal(str(hour)), "close": Decimal(str(hour)),
            "volume": Decimal("1"),
        })

    latest = store.get_candles("BTC/USDT", "1h", limit=3)
    assert [c["close"] for c in latest] == [
        Decimal("4"), Decimal("5"), Decimal("6")
    ]
    assert [c["candle_close_utc"] for c in latest] == [
        "2026-10-11T04:00:00+00:00",
        "2026-10-11T05:00:00+00:00",
        "2026-10-11T06:00:00+00:00",
    ]
    assert store.get_candles("ETH/USDT", "1h", limit=3) == []
