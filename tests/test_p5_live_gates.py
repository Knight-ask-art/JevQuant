from decimal import Decimal
from unittest.mock import MagicMock
import pytest
from jevquant.config import AppConfig
from jevquant.exchange import AccountBalance
from jevquant.runner import BinanceLiveAdapter, initialize_runner, run_live_preflight


def test_binance_live_adapter_requires_keys():
    with pytest.raises(ValueError, match="Live API key and secret are required"):
        BinanceLiveAdapter(api_key="", api_secret="")


def test_run_live_preflight_checks_pass():
    mock_adapter = MagicMock()
    mock_adapter.fetch_market_rules.return_value = {
        "symbol": "BTC/USDT",
        "status": "TRADING",
        "min_notional": Decimal("5.00"),
        "step_size": Decimal("0.00001"),
        "min_qty": Decimal("0.00001"),
    }
    mock_adapter.fetch_balance.return_value = AccountBalance(
        free_usdt=Decimal("8.50"),
        locked_usdt=Decimal("0"),
        free_btc=Decimal("0"),
        locked_btc=Decimal("0"),
        total_usdt=Decimal("8.50"),
        timestamp_utc="2026-10-11T00:00:00Z",
    )

    report = run_live_preflight(
        adapter=mock_adapter,
        capital_cap_usdt=Decimal("7.00"),
        symbol="BTC/USDT",
    )
    assert report.passed is True
    assert report.free_usdt == Decimal("8.50")
    assert report.capital_cap_usdt == Decimal("7.00")
    assert len(report.reasons) == 0


def test_run_live_preflight_fails_when_balance_below_min_notional():
    mock_adapter = MagicMock()
    mock_adapter.fetch_market_rules.return_value = {
        "symbol": "BTC/USDT",
        "status": "TRADING",
        "min_notional": Decimal("5.00"),
        "step_size": Decimal("0.00001"),
        "min_qty": Decimal("0.00001"),
    }
    # Free balance 3.00 < 5.00 min_notional
    mock_adapter.fetch_balance.return_value = AccountBalance(
        free_usdt=Decimal("3.00"),
        locked_usdt=Decimal("0"),
        free_btc=Decimal("0"),
        locked_btc=Decimal("0"),
        total_usdt=Decimal("3.00"),
        timestamp_utc="2026-10-11T00:00:00Z",
    )

    report = run_live_preflight(
        adapter=mock_adapter,
        capital_cap_usdt=Decimal("7.00"),
        symbol="BTC/USDT",
    )
    assert report.passed is False
    assert any("below minimum notional" in r for r in report.reasons)


def test_run_live_preflight_fails_when_capital_exceeds_50cny_cap():
    mock_adapter = MagicMock()
    mock_adapter.fetch_market_rules.return_value = {
        "symbol": "BTC/USDT",
        "status": "TRADING",
        "min_notional": Decimal("5.00"),
        "step_size": Decimal("0.00001"),
        "min_qty": Decimal("0.00001"),
    }
    mock_adapter.fetch_balance.return_value = AccountBalance(
        free_usdt=Decimal("50.00"),
        locked_usdt=Decimal("0"),
        free_btc=Decimal("0"),
        locked_btc=Decimal("0"),
        total_usdt=Decimal("50.00"),
        timestamp_utc="2026-10-11T00:00:00Z",
    )

    # 15.00 USDT > 10.00 USDT safety cap
    report = run_live_preflight(
        adapter=mock_adapter,
        capital_cap_usdt=Decimal("15.00"),
        symbol="BTC/USDT",
    )
    assert report.passed is False
    assert any("exceeds 50 CNY safety limit" in r for r in report.reasons)


def test_initialize_runner_paper_mode_safe():
    runner = initialize_runner(cli_confirm_live=False, pause_new_entries=True)
    assert runner["config"].trading_mode == "paper"
    assert runner["executor"].mode == "paper"
    assert runner["risk"].is_halted is True
    assert runner["risk"].halt_reason == "MANUAL_PAUSE_TRIGGERED"
