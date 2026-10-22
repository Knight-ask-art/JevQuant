"""PostgreSQL integration coverage for the BUY counterfactual cohort query.

This intentionally does not run against the user's configured trading DB.
The dedicated disposable PostgreSQL CI database is required.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from jev_trading.domain.enums import SignalState
from jev_trading.storage.db import Database
from jev_trading.storage.models import Base
from jev_trading.storage.repository import Repository


@pytest.fixture
async def pg_repo():
    url = os.getenv("JEV_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires disposable JEV_TEST_POSTGRES_URL")
    db = Database(url)
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield Repository(db)
    finally:
        async with db.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await db.dispose()


async def _signal(repo: Repository, *, choice: str, state: SignalState):
    signal = await repo.create_signal(
        symbol="BTCUSDT",
        policy_id="policy-ci",
        snapshot_id="snapshot-ci",
        side="buy",
        state=state,
    )
    await repo.attach_jev_evaluation(
        signal.id,
        "evaluation-ci",
        score={
            "entry_choice": choice,
            "entry_probability": 0.8,
            "policy_fit": 0.9,
            "quality_score": 2.0,
        },
    )
    return signal


async def test_postgres_json_extract_and_correlated_gate_event_exists(pg_repo):
    rejected = await _signal(pg_repo, choice="buy", state=SignalState.REJECTED)
    eligible = await _signal(pg_repo, choice="buy", state=SignalState.ELIGIBLE)
    await _signal(pg_repo, choice="reject", state=SignalState.REJECTED)
    await _signal(pg_repo, choice="buy", state=SignalState.SCORED)
    now = datetime.now(UTC)
    rows = await pg_repo.list_decided_buy_signals_without_counterfactual(
        since=now - timedelta(minutes=5),
        until=now + timedelta(minutes=5),
    )
    assert {row.id for row in rows} == {rejected.id, eligible.id}

    # This signal must remain in the eligible cohort even after risk rejection.
    await pg_repo.transition_signal(eligible.id, SignalState.INTENT_CREATED)
    await pg_repo.transition_signal(eligible.id, SignalState.RISK_PENDING)
    await pg_repo.transition_signal(eligible.id, SignalState.RISK_REJECTED)
    rows = await pg_repo.list_decided_buy_signals_without_counterfactual(
        since=now - timedelta(minutes=5),
        until=now + timedelta(minutes=5),
    )
    assert {row.id for row in rows} == {rejected.id, eligible.id}


async def test_postgres_counterfactual_unique_signal_is_idempotent(pg_repo):
    signal = await _signal(pg_repo, choice="buy", state=SignalState.ELIGIBLE)
    fields = {
        "signal_id": signal.id,
        "policy_id": signal.policy_id,
        "symbol": signal.symbol,
        "decided_at": signal.created_at,
        "entry_choice": "buy",
        "entry_probability": 0.8,
        "policy_fit": 0.9,
        "quality_score": 2.0,
        "blocked_rule": "jev_gate_passed",
        "assumed_notional_usdt": Decimal("10"),
        "entry_price": Decimal("100"),
        "exit_price": Decimal("101"),
        "exit_reason": "timeout",
        "net_bps": Decimal("70"),
        "pnl_usdt": Decimal("0.07"),
        "horizon_seconds": 3600,
        "gap_seconds": 0,
        "method": "1m-conservative-v1",
    }
    first, second = await asyncio.gather(
        pg_repo.insert_counterfactual(**fields),
        pg_repo.insert_counterfactual(**fields),
    )
    assert first.signal_id == second.signal_id == signal.id
    assert len(await pg_repo.list_counterfactuals()) == 1
    assert await pg_repo.list_decided_buy_signals_without_counterfactual(
        since=signal.created_at - timedelta(minutes=1),
        until=signal.created_at + timedelta(minutes=1),
    ) == []
