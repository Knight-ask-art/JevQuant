"""The fill read model that the realised-cost report is built on.

A fee is only meaningful next to the notional it was charged on, and slippage
is only meaningful next to the quote the decision was made on. Both come from
one query, so a join that silently dropped the snapshot would turn every
slippage measurement into "unknown" without anyone noticing.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import compiled_policy, make_context, make_intent

from jev_trading.domain.enums import Mode
from jev_trading.storage.db import Database
from jev_trading.storage.models import OrderRow
from jev_trading.storage.repository import Repository


@pytest.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'costs.db'}")
    await db.create_all()
    try:
        yield Repository(db)
    finally:
        await db.dispose()


async def test_the_fill_query_brings_back_the_quote_the_decision_used(
    repo, server_limits
):
    account = await repo.get_or_create_account("main", Mode.PAPER, Decimal("10000"))
    policy = compiled_policy(server_limits)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    ctx = make_context(policy)
    await repo.insert_market_snapshot(ctx.market)
    signal = await repo.create_signal(
        symbol=ctx.market.symbol,
        policy_id=str(policy.policy_id),
        snapshot_id=str(ctx.market.snapshot_id),
        side="BUY",
    )
    intent = make_intent(
        policy, ctx, account_id=account.id, signal_id=signal.id, mode=Mode.TESTNET
    )
    await repo.insert_intent(intent, status="EXECUTION_PENDING")
    order_id = str(uuid4())
    async with repo.db.session() as session:
        session.add(
            OrderRow(
                id=order_id,
                intent_id=str(intent.intent_id),
                exchange_order_id="venue-1",
                symbol=ctx.market.symbol,
                side="BUY",
                order_type="MARKET",
                qty=Decimal("0.1"),
                price=None,
                status="FILLED",
                filled_qty=Decimal("0.1"),
                avg_fill_price=Decimal("68000"),
                fee_quote=Decimal("6.8"),
                mode="testnet",
                is_simulated=True,
            )
        )
        await session.commit()
    await repo.add_exchange_fill(
        order_id=order_id,
        trade_id="trade-1",
        qty=Decimal("0.1"),
        price=Decimal("68000"),
        fee_quote=Decimal("6.8"),
        fee_asset="USDT",
        fee_amount=Decimal("6.8"),
    )

    fills = await repo.list_realised_fills()

    assert len(fills) == 1
    fill = fills[0]
    assert fill.symbol == ctx.market.symbol
    assert fill.side == "BUY"
    assert fill.intent_type == "OPEN"
    assert fill.mode == "testnet"
    assert fill.fee_amount == Decimal("6.8")
    # A buy is judged against the ask it was decided on, not a later quote.
    assert fill.reference_price == ctx.market.ask
