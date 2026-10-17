"""Binance Testnet ledger reconciliation.

The testnet adapter has never talked to a real venue from this repository, so
these tests drive it against a stub transport that answers like
``GET /api/v3/order``. What matters here is the local bookkeeping: an exchange
close that filled while the ledger had it resting must become a closed
position with realized PnL and cash, and a second reconciliation pass must not
double-count it.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from conftest import compiled_policy, make_context, make_intent, system_snapshot
from jev_trading.adapters.execution.binance_testnet import (
    STATUS_MAP,
    BinanceTestnetExecutionAdapter,
)
from jev_trading.domain.enums import (
    IntentType,
    Mode,
    OrderPreference,
    OrderStatus,
    Side,
    SignalState,
)
from jev_trading.domain.money import ZERO
from jev_trading.domain.risk import RiskEngine, compute_risk_snapshot_hash
from jev_trading.storage.db import Database
from jev_trading.storage.models import OrderRow, PositionRow
from jev_trading.storage.repository import ExecutionFill, Repository


@pytest.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'testnet.db'}")
    await db.create_all()
    try:
        yield Repository(db)
    finally:
        await db.dispose()


class StubTransport:
    """Replays queued ``GET /api/v3/order`` responses; records nothing else."""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    async def request(self, method, path, params=None):
        return await self._request(method, path, params)

    async def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        self.calls.append((method, path, dict(params or {})))
        return self.responses.pop(0)


def make_adapter(repo, responses: list[dict]) -> BinanceTestnetExecutionAdapter:
    adapter = BinanceTestnetExecutionAdapter(
        repo, base_url="https://testnet.invalid", api_key="k", api_secret="s"
    )
    adapter._request = StubTransport(responses)._request  # type: ignore[method-assign]
    return adapter


async def open_testnet_position(repo, server_limits, *, entry_fee: Decimal):
    """An OPEN testnet position whose close order is still ACCEPTED locally."""

    account = await repo.get_or_create_account("main", Mode.TESTNET, Decimal("10000"))
    policy = compiled_policy(server_limits, mode_scope=(Mode.PAPER, Mode.TESTNET))
    system = system_snapshot(mode=Mode.TESTNET)
    ctx = make_context(policy, system=system)
    engine = RiskEngine()
    signal = await repo.create_signal(
        symbol="BTCUSDT",
        policy_id=str(policy.policy_id),
        snapshot_id=str(ctx.market.snapshot_id),
        side="BUY",
    )
    intent = make_intent(
        policy, ctx, account_id=account.id, signal_id=signal.id, mode=Mode.TESTNET
    )
    decision = engine.evaluate(intent, ctx)
    assert decision.approved, decision.failed_rule_names
    qty = decision.approved_intent.approved_qty
    # A marketable limit at the snapshot ask, so the ledger records exactly the
    # price this helper assumes.
    entry_intent = intent.model_copy(
        update={
            "order_preference": OrderPreference.MARKET,
            "limit_price": None,
            "risk_snapshot_hash": "pending",
        }
    )
    entry_ctx = make_context(policy, system=system)
    entry_intent = entry_intent.model_copy(
        update={"risk_snapshot_hash": compute_risk_snapshot_hash(entry_intent, entry_ctx)}
    )
    entry_decision = engine.evaluate(entry_intent, entry_ctx)
    assert entry_decision.approved, entry_decision.failed_rule_names
    await repo.insert_intent(entry_intent, status="EXECUTION_PENDING")
    await repo.insert_risk_decision(entry_decision)
    entry_price = ctx.market.ask
    # Start from a clean venue balance. accounts.mode has no default, so the
    # ORM insert leaves it NULL and any update on that row would fail.
    async with repo.db.session() as session:
        from sqlalchemy import text

        await session.execute(
            text(
                "update trading_accounts set cash_quote_usdt = 10000, "
                "peak_equity_usdt = 10000, realized_pnl_usdt = 0, "
                "daily_realized_pnl_usdt = 0, mode = 'testnet' where id = :id"
            ),
            {"id": account.id},
        )
    _order, position = await repo.record_execution(
        approved=entry_decision.approved_intent,
        order_id=str(uuid4()),
        exchange_order_id="1001",
        status=OrderStatus.FILLED,
        fills=[ExecutionFill(qty=qty, price=entry_price, fee_quote=entry_fee)],
        account_id=account.id,
    )
    cash_after_entry = (await repo.get_account(account.id)).cash_quote_usdt
    entry_fill_price = _order.avg_fill_price
    for state in (
        SignalState.JEV_EVALUATING,
        SignalState.SCORED,
        SignalState.ELIGIBLE,
        SignalState.INTENT_CREATED,
        SignalState.RISK_PENDING,
        SignalState.APPROVED,
        SignalState.EXECUTION_PENDING,
        SignalState.ACCEPTED,
        SignalState.FILLED,
        SignalState.POSITION_OPEN,
    ):
        await repo.transition_signal(signal.id, state)

    # A close intent whose order the venue reports FILLED but the ledger
    # recorded as ACCEPTED (the unknown outcome a resting order can leave).
    close_ctx = make_context(
        policy, market=ctx.market, position_qty=str(qty), system=system
    )
    close_intent = make_intent(
        policy,
        close_ctx,
        intent_type=IntentType.CLOSE,
        side=Side.SELL,
        quote_notional=str(qty * close_ctx.market.bid),
        limit_price=str(close_ctx.market.bid),
        account_id=account.id,
        signal_id=signal.id,
        idempotency="close-testnet-0001",
        mode=Mode.TESTNET,
    )
    close_decision = engine.evaluate(close_intent, close_ctx)
    assert close_decision.approved, close_decision.failed_rule_names
    await repo.insert_intent(close_intent, status="EXECUTION_PENDING")
    await repo.insert_risk_decision(close_decision)
    close_order, _ = await repo.record_execution(
        approved=close_decision.approved_intent,
        order_id=str(uuid4()),
        exchange_order_id="2002",
        status=OrderStatus.ACCEPTED,
        fills=[],
        account_id=account.id,
    )
    await repo.update_intent_status(str(close_intent.intent_id), "ACCEPTED")
    await repo.transition_signal(signal.id, SignalState.EXIT_PENDING)
    return (
        account,
        policy,
        signal,
        position,
        close_order,
        (cash_after_entry, entry_fill_price),
    )


def venue_close(order_id: str, qty: str, price: str, fee: str, trade_id: int) -> dict:
    return {
        "orderId": 2002,
        "status": "FILLED",
        "executedQty": qty,
        "cummulativeQuoteQty": str(Decimal(qty) * Decimal(price)),
        "fills": [
            {
                "id": trade_id,
                "price": price,
                "qty": qty,
                "commission": fee,
                "commissionAsset": "USDT",
            }
        ],
    }


async def ledger_totals(repo, account_id: str) -> tuple[Decimal, Decimal]:
    """(realized_pnl_usdt, sum of position realized PnL) straight from the rows.

    The account snapshot is derived on read, so assertions that must see the
    committed ledger read the tables instead of the ORM object.
    """

    async with repo.db.session() as session:
        from sqlalchemy import text

        account = (
            await session.execute(
                text("select realized_pnl_usdt from trading_accounts where id = :id"),
                {"id": account_id},
            )
        ).first()[0]
        positions = (
            await session.execute(
                text(
                    "select coalesce(sum(realized_pnl_usdt), 0) from positions "
                    "where account_id = :id"
                ),
                {"id": account_id},
            )
        ).first()[0]
        return Decimal(str(account)), Decimal(str(positions))


async def test_exchange_close_rebuilds_position_cash_and_pnl(repo, server_limits):
    account, _policy, signal, position, close_order, (cash_before, entry_ask) = (
        await open_testnet_position(repo, server_limits, entry_fee=Decimal("0.68"))
    )
    entry_fee = position.entry_order_id and (
        (await repo.get_order(position.entry_order_id)).fee_quote
    )
    before_realized, before_positions = await ledger_totals(repo, account.id)
    # The venue prints well above entry, so the round trip is a winner.
    qty, fill_fee = position.qty, Decimal("0.0699")
    exit_price = Decimal("68500")
    adapter = make_adapter(
        repo,
        [
            {
                "orderId": 2002,
                "status": "FILLED",
                "executedQty": str(qty),
                "cummulativeQuoteQty": str(qty * exit_price),
                "fills": None,  # GET schema; fees come from the trade stream
            }
        ],
    )
    await repo.add_exchange_fill(
        order_id=close_order.id,
        trade_id="77",
        qty=qty,
        price=exit_price,
        fee_quote=fill_fee,
    )
    result = await adapter.reconcile(account.id)
    assert result.healthy is True, result.notes

    refreshed = await repo.settled_position(position.id)
    assert refreshed.status == "CLOSED"
    assert refreshed.qty == ZERO
    assert refreshed.exit_order_id == close_order.id

    expected_realized = qty * (exit_price - entry_ask) - fill_fee - entry_fee
    after_realized, after_positions = await ledger_totals(repo, account.id)
    assert after_realized - before_realized == expected_realized
    assert after_positions - before_positions == expected_realized
    assert (await repo.get_account(account.id)).cash_quote_usdt == (
        cash_before + qty * exit_price - fill_fee
    )
    assert (await repo.get_signal(signal.id)).state == SignalState.CLOSED.value
    assert (await repo.get_order(close_order.id)).status == OrderStatus.FILLED.value


async def test_reconcile_is_idempotent(repo, server_limits):
    account, _policy, _signal, position, _close_order, _cash = await open_testnet_position(
        repo, server_limits, entry_fee=Decimal("0.1")
    )
    adapter = make_adapter(
        repo,
        [venue_close(_close_order.id, str(position.qty), "68100", "0.05", 11)],
    )
    await adapter.reconcile(account.id)
    first = await repo.get_account(account.id)
    await adapter.reconcile(account.id)
    second = await repo.get_account(account.id)

    assert second.cash_quote_usdt == first.cash_quote_usdt
    assert second.realized_pnl_usdt == first.realized_pnl_usdt
    fills = await repo.list_fills_for_order(_close_order.id)
    assert len(fills) == 1


async def test_unknown_trade_schema_leaves_the_position_for_a_human(repo, server_limits):
    account, _policy, signal, position, close_order, _cash = await open_testnet_position(
        repo, server_limits, entry_fee=Decimal("0.1")
    )
    adapter = make_adapter(
        repo,
        [
            {
                "orderId": 2002,
                "status": "FILLED",
                "executedQty": "0.01",
                "cummulativeQuoteQty": "681",
                "fills": [{"tradeId": 5, "commission": "0.05", "commissionAsset": "USDT"}],
            }
        ],
    )
    result = await adapter.reconcile(account.id)
    assert result.healthy is False
    # Nothing is guessed: the order and the position stay as they were.
    assert (await repo.get_order(close_order.id)).status == OrderStatus.ACCEPTED.value
    assert (await repo.settled_position(position.id)).status == "OPEN"
    assert (await repo.get_signal(signal.id)).state == SignalState.EXIT_PENDING.value
    before = await ledger_totals(repo, account.id)
    assert await ledger_totals(repo, account.id) == before
