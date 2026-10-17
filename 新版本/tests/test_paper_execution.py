"""Paper execution is the same contract as live, with a local fill model."""

from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import (
    NOW,
    compiled_policy,
    make_context,
    make_intent,
    symbol_rules,
)
from jev_trading.adapters.execution.paper import PaperExecutionAdapter
from jev_trading.domain.enums import Mode, OrderStatus, PositionStatus
from jev_trading.domain.risk import RiskEngine
from jev_trading.storage.db import Database
from jev_trading.storage.repository import Repository


@pytest.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'paper.db'}")
    await db.create_all()
    try:
        yield Repository(db)
    finally:
        await db.dispose()


async def test_paper_fill_updates_order_fill_position_and_cash(repo, server_limits):
    account = await repo.get_or_create_account("main", Mode.PAPER, Decimal("10000"))
    policy = compiled_policy(server_limits)
    ctx = make_context(policy)
    intent = make_intent(policy, ctx, account_id=account.id)
    decision = RiskEngine().evaluate(intent, ctx)
    assert decision.approved

    await repo.insert_intent(intent, status="EXECUTION_PENDING")
    await repo.insert_risk_decision(decision)
    adapter = PaperExecutionAdapter(repo, seed=7)
    receipt = await adapter.submit(decision.approved_intent, ctx.market, ctx.symbol_rules)
    assert receipt.status is OrderStatus.FILLED
    assert receipt.filled_qty > 0
    assert receipt.fee_quote > 0
    assert receipt.position_id is not None

    order = await repo.get_order(receipt.order_id)
    assert order is not None
    assert order.status == OrderStatus.FILLED.value
    assert order.filled_qty == receipt.filled_qty

    fills = await repo.list_fills(limit=10)
    assert len(fills) == 1
    assert fills[0].order_id == receipt.order_id

    positions = await repo.get_open_positions(account.id)
    assert len(positions) == 1
    assert positions[0].status == PositionStatus.OPEN.value
    assert positions[0].qty == receipt.filled_qty
    assert positions[0].is_simulated is True

    snapshot = await repo.account_snapshot(account.id, {"BTCUSDT": ctx.market.mid}, NOW)
    assert snapshot.available_quote_usdt < Decimal("10000")
    assert snapshot.open_positions == 1
    assert snapshot.gross_exposure_usdt > 0


async def test_resting_limit_buy_is_persisted_as_accepted(repo, server_limits):
    account = await repo.get_or_create_account(
        "main", Mode.PAPER, Decimal("10000")
    )
    policy = compiled_policy(server_limits)
    ctx = make_context(policy)
    intent = make_intent(policy, ctx, limit_price="60000", account_id=account.id)
    decision = RiskEngine().evaluate(intent, ctx)
    assert decision.approved

    await repo.insert_intent(intent, status="EXECUTION_PENDING")
    await repo.insert_risk_decision(decision)
    adapter = PaperExecutionAdapter(repo, seed=7)
    receipt = await adapter.submit(decision.approved_intent, ctx.market, ctx.symbol_rules)
    assert receipt.status is OrderStatus.ACCEPTED
    order = await repo.get_order(receipt.order_id)
    assert order is not None
    assert order.status == OrderStatus.ACCEPTED.value
    assert order.filled_qty == Decimal("0")
    assert await repo.get_open_positions(account.id) == []


async def test_paper_health_and_reconcile(repo):
    adapter = PaperExecutionAdapter(repo)
    health = await adapter.health()
    assert health["healthy"] is True
    assert health["backend"] == "paper"
    account = await repo.get_or_create_account("main", Mode.PAPER, Decimal("10000"))
    result = await adapter.reconcile(account.id)
    assert result.healthy is True
