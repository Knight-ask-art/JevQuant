"""A paced symbol leaves no signal behind, and never an unresolvable state."""

from __future__ import annotations

import pytest
from conftest import NOW, compiled_policy, market_snapshot

from jev_trading.domain.enums import Mode
from jev_trading.services.orchestrator import TradingOrchestrator
from jev_trading.storage.db import Database
from jev_trading.storage.repository import Repository


@pytest.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'pacing.db'}")
    await db.create_all()
    try:
        yield Repository(db)
    finally:
        await db.dispose()


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    async def record(self, event, entity_type, entity_id, detail=None):
        self.events.append((event, entity_type, entity_id, detail))


class FakeBroker:
    async def publish(self, *_args, **_kwargs) -> None:
        return None


async def test_a_paced_symbol_creates_no_signal(repo, server_limits, settings):
    policy = compiled_policy(server_limits)
    audit = FakeAudit()
    orchestrator = TradingOrchestrator(
        settings=settings,
        repo=repo,
        policy_service=None,
        market=None,
        jev=None,
        policy_generator=None,
        risk_engine=None,
        execution_for_mode={},
        modes=None,
        audit=audit,
        broker=FakeBroker(),
        account_id="a1",
    )

    async def still_paced(symbol: str, now) -> float:
        return 30.0

    orchestrator._jev_pace_wait = still_paced
    counters = {"entries_blocked": 0, "signals_created": 0}
    notes: list[str] = []

    await orchestrator._process_entry(
        mode=Mode.PAPER,
        policy=policy,
        policy_row=None,
        symbol_policy=policy.universe[0],
        snapshot=market_snapshot(NOW),
        snapshot_map={},
        system=None,
        adapter=None,
        counters=counters,
        notes=notes,
    )

    assert counters["entries_blocked"] == 1
    assert counters["signals_created"] == 0
    assert await repo.list_signals() == []
    assert [event[0] for event in audit.events] == ["signal.paced"]
    assert audit.events[0][1] == "symbol"
    assert "paced" in notes[0]
