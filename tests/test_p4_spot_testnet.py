from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from jevquant.exchange import AccountBalance, BinancePublicAdapter, BinanceTestnetAdapter, ExecutionReport
from jevquant.orders import OrderExecutor, OrderStateMachine
from jevquant.storage import Storage


def test_public_adapter_blocks_private_order_and_balance_calls():
    mock_ex = MagicMock()
    adapter = BinancePublicAdapter(exchange=mock_ex)

    with pytest.raises(PermissionError, match="strictly prohibits order placement"):
        adapter.create_order(
            symbol="BTC/USDT",
            side="BUY",
            order_type="MARKET",
            quantity=Decimal("0.0001"),
            client_order_id="ord_123",
        )

    with pytest.raises(NotImplementedError, match="no private API keys"):
        adapter.fetch_balance()


def test_testnet_adapter_requires_keys():
    with pytest.raises(ValueError, match="Testnet API key and secret are required"):
        BinanceTestnetAdapter(api_key="", api_secret="")


def test_testnet_adapter_create_and_fetch_order():
    mock_ex = MagicMock()
    mock_ex.create_order.return_value = {
        "id": "1001",
        "clientOrderId": "ord_test_001",
        "status": "closed",
        "filled": 0.0001,
        "average": 60000.0,
        "fee": {"cost": 0.006, "currency": "USDT"},
    }
    adapter = BinanceTestnetAdapter(api_key="test_key", api_secret="test_secret", exchange=mock_ex)

    report = adapter.create_order(
        symbol="BTC/USDT",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.0001"),
        client_order_id="ord_test_001",
    )
    assert report.client_order_id == "ord_test_001"
    assert report.exchange_order_id == "1001"
    assert report.filled_qty == Decimal("0.0001")
    assert report.status == "CLOSED"
    assert report.fee_amount == Decimal("0.006")


def test_order_executor_idempotency_and_state_transition(tmp_path: Path):
    db_file = tmp_path / "testnet.db"
    store = Storage(db_file)
    mock_adapter = MagicMock()
    mock_adapter.create_order.return_value = ExecutionReport(
        client_order_id="ord_mock_1",
        exchange_order_id="999",
        symbol="BTC/USDT",
        side="BUY",
        order_type="MARKET",
        status="FILLED",
        target_qty=Decimal("0.0001"),
        filled_qty=Decimal("0.0001"),
        avg_price=Decimal("60000.0"),
        timestamp_utc="2026-10-11T00:00:00Z",
    )

    sm = OrderStateMachine(initial_state="FLAT")
    executor = OrderExecutor(adapter=mock_adapter, storage=store, state_machine=sm, mode="testnet")

    rep = executor.execute_market_order(
        symbol="BTC/USDT",
        side="BUY",
        quantity=Decimal("0.0001"),
        signal_id="sig_test_100",
    )

    assert rep.status == "FILLED"
    assert sm.state == "LONG"
    # Order saved in SQLite
    saved_ord = store.get_order(rep.client_order_id)
    assert saved_ord is not None
    assert saved_ord["status"] == "FILLED"


def test_order_executor_timeout_handles_idempotent_query(tmp_path: Path):
    db_file = tmp_path / "testnet_timeout.db"
    store = Storage(db_file)
    mock_adapter = MagicMock()
    # create_order times out, but fetch_order shows it filled!
    mock_adapter.create_order.side_effect = TimeoutError("Network timeout on Binance")
    mock_adapter.fetch_order.return_value = ExecutionReport(
        client_order_id="ord_recovered",
        exchange_order_id="888",
        symbol="BTC/USDT",
        side="BUY",
        order_type="MARKET",
        status="FILLED",
        target_qty=Decimal("0.0001"),
        filled_qty=Decimal("0.0001"),
        avg_price=Decimal("60000.0"),
        timestamp_utc="2026-10-11T00:00:00Z",
    )

    sm = OrderStateMachine(initial_state="FLAT")
    executor = OrderExecutor(adapter=mock_adapter, storage=store, state_machine=sm, mode="testnet")

    # Should safely query and resolve without throwing blind retry
    rep = executor.execute_market_order(
        symbol="BTC/USDT",
        side="BUY",
        quantity=Decimal("0.0001"),
        signal_id="sig_timeout_test",
    )
    assert rep.status == "FILLED"
    assert sm.state == "LONG"


def test_startup_reconciliation_recovers_state(tmp_path: Path):
    db_file = tmp_path / "testnet_reconcile.db"
    store = Storage(db_file)

    # Insert an unconfirmed order in DB
    store.save_order({
        "client_order_id": "ord_unconfirmed_01",
        "signal_id": "sig_1",
        "mode": "testnet",
        "symbol": "BTC/USDT",
        "side": "BUY",
        "order_type": "MARKET",
        "status": "ENTRY_SUBMITTING",
        "target_qty": Decimal("0.0001"),
        "created_at_utc": "2026-10-11T00:00:00Z",
        "updated_at_utc": "2026-10-11T00:00:00Z",
    })

    mock_adapter = MagicMock()
    # Exchange says it was FILLED
    mock_adapter.fetch_order.return_value = ExecutionReport(
        client_order_id="ord_unconfirmed_01",
        exchange_order_id="555",
        symbol="BTC/USDT",
        side="BUY",
        order_type="MARKET",
        status="FILLED",
        target_qty=Decimal("0.0001"),
        filled_qty=Decimal("0.0001"),
        avg_price=Decimal("60000.0"),
        timestamp_utc="2026-10-11T00:00:00Z",
    )

    sm = OrderStateMachine(initial_state="FLAT")
    executor = OrderExecutor(adapter=mock_adapter, storage=store, state_machine=sm, mode="testnet")

    executor.reconcile_on_startup("BTC/USDT")
    assert sm.state == "LONG"
    updated_ord = store.get_order("ord_unconfirmed_01")
    assert updated_ord["status"] == "FILLED"
