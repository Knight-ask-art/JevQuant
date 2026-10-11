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
    assert report.status == "FILLED"
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


def test_ccxt_closed_status_advances_executor_to_long(tmp_path: Path):
    store = Storage(tmp_path / "ccxt_closed.db")
    mock_ex = MagicMock()
    mock_ex.create_order.return_value = {
        "id": "101", "status": "closed", "filled": 0.0001, "average": 60000.0
    }
    adapter = BinanceTestnetAdapter(api_key="test", api_secret="test", exchange=mock_ex)
    executor = OrderExecutor(adapter=adapter, storage=store, mode="testnet")
    report = executor.execute_market_order("BTC/USDT", "BUY", Decimal("0.0001"), "sig_closed")
    assert report.status == "FILLED"
    assert executor.sm.state == "LONG"
    assert store.get_order(report.client_order_id)["status"] == "FILLED"


def test_canceled_partial_fill_must_require_reconciliation(tmp_path: Path):
    store = Storage(tmp_path / "partial_cancel.db")
    mock_ex = MagicMock()
    mock_ex.create_order.return_value = {
        "id": "102", "status": "canceled", "filled": 0.00005, "average": 60000.0
    }
    adapter = BinanceTestnetAdapter(api_key="test", api_secret="test", exchange=mock_ex)
    executor = OrderExecutor(adapter=adapter, storage=store, mode="testnet")
    report = executor.execute_market_order("BTC/USDT", "BUY", Decimal("0.0001"), "sig_partial")
    assert report.status == "PARTIALLY_FILLED"
    assert executor.sm.state == "RECONCILIATION_REQUIRED"


@pytest.mark.parametrize(("initial_state", "side"), [
    ("LONG", "BUY"),
    ("FLAT", "SELL"),
    ("ENTRY_SUBMITTING", "BUY"),
    ("EXIT_SUBMITTING", "SELL"),
    ("HALTED", "BUY"),
    ("RECONCILIATION_REQUIRED", "SELL"),
])
def test_executor_rejects_orders_in_invalid_states(tmp_path: Path, initial_state: str, side: str):
    store = Storage(tmp_path / "invalid_state.db")
    adapter = MagicMock()
    sm = OrderStateMachine(initial_state=initial_state)
    executor = OrderExecutor(adapter=adapter, storage=store, state_machine=sm)

    with pytest.raises(RuntimeError, match="Order execution blocked"):
        executor.execute_market_order("BTC/USDT", side, Decimal("0.0001"), "sig_bad")

    assert sm.state == initial_state
    adapter.create_order.assert_not_called()
    assert store.get_unreconciled_orders() == []


@pytest.mark.parametrize(("side", "quantity"), [
    ("HOLD", Decimal("0.0001")),
    ("buy", Decimal("0.0001")),
    ("BUY", Decimal("0")),
    ("BUY", Decimal("-0.001")),
    ("BUY", Decimal("NaN")),
    ("BUY", Decimal("Infinity")),
])
def test_executor_rejects_invalid_intent_before_submission(
    tmp_path: Path, side: str, quantity: Decimal
):
    store = Storage(tmp_path / "invalid_intent.db")
    adapter = MagicMock()
    executor = OrderExecutor(adapter=adapter, storage=store)
    with pytest.raises(ValueError):
        executor.execute_market_order("BTC/USDT", side, quantity, "sig_invalid")
    assert executor.sm.state == "FLAT"
    adapter.create_order.assert_not_called()
    assert store.get_unreconciled_orders() == []


def test_executor_allows_sell_from_long_position(tmp_path: Path):
    store = Storage(tmp_path / "valid_exit.db")
    adapter = MagicMock()
    adapter.create_order.return_value = ExecutionReport(
        client_order_id="placeholder", exchange_order_id="100",
        symbol="BTC/USDT", side="SELL", order_type="MARKET",
        status="FILLED", target_qty=Decimal("0.0001"),
        filled_qty=Decimal("0.0001"),
        timestamp_utc="2026-10-11T00:00:00Z",
    )
    sm = OrderStateMachine(initial_state="LONG")
    executor = OrderExecutor(adapter=adapter, storage=store, state_machine=sm)
    report = executor.execute_market_order(
        "BTC/USDT", "SELL", Decimal("0.0001"), "sig_exit"
    )
    assert report.status == "FILLED"
    assert sm.state == "FLAT"
    adapter.create_order.assert_called_once()
