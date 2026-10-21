"""A paced symbol leaves no signal behind, and never an unresolvable state."""

from __future__ import annotations

from datetime import timedelta

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


async def test_a_rejected_signal_still_paces_the_symbol(repo, server_limits, settings):
    from conftest import NOW
    from conftest import market_snapshot as snap

    from jev_trading.domain.enums import SignalState

    policy = compiled_policy(server_limits)
    snapshot = snap(NOW)
    await repo.insert_market_snapshot(snapshot)
    signal = await repo.create_signal(
        symbol="BTCUSDT",
        policy_id=str(policy.policy_id),
        snapshot_id=str(snapshot.snapshot_id),
        side="buy",
        created_at=NOW,
    )
    await repo.transition_signal(signal.id, SignalState.JEV_EVALUATING)
    await repo.transition_signal(signal.id, SignalState.SCORED)
    await repo.transition_signal(signal.id, SignalState.REJECTED, {"reason": "test"})

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
        audit=FakeAudit(),
        broker=FakeBroker(),
        account_id="a1",
    )

    # The call was paid for: a rejection must hold the symbol back too.
    wait = await orchestrator._jev_pace_wait("BTCUSDT", NOW + timedelta(minutes=5))
    assert wait > 0
    assert wait == pytest.approx(settings.jev_min_call_interval_seconds - 300, abs=1)


async def test_a_budget_refusal_does_not_start_the_cooldown(
    repo, server_limits, settings, monkeypatch
):
    """A call Jev never received must not cost the symbol an hour."""

    from conftest import market_snapshot as snap

    from jev_trading.domain.enums import SignalState
    from jev_trading.domain.errors import BudgetExhaustedError

    policy = compiled_policy(server_limits)
    snapshot = snap(NOW)
    await repo.insert_market_snapshot(snapshot)

    class RefusingBudget:
        async def check_jev(self, *_args, **_kwargs):
            raise BudgetExhaustedError("Jev daily call budget is spent.")

    class NoCandles:
        async def klines(self, *_args, **_kwargs):
            return []

    class NoJev:
        async def evaluate(self, *_args, **_kwargs):
            raise AssertionError("Jev must not be called when the budget is spent")

    orchestrator = TradingOrchestrator(
        settings=settings,
        repo=repo,
        policy_service=None,
        market=NoCandles(),
        jev=NoJev(),
        policy_generator=None,
        risk_engine=None,
        execution_for_mode={},
        modes=None,
        audit=FakeAudit(),
        broker=FakeBroker(),
        account_id="a1",
        budgets=RefusingBudget(),
    )

    async def free(symbol: str, now) -> float:
        return 0.0

    orchestrator._jev_pace_wait = free
    counters = {"entries_blocked": 0, "signals_created": 0, "errors": 0}
    notes: list[str] = []

    await orchestrator._process_entry(
        mode=Mode.PAPER,
        policy=policy,
        policy_row=None,
        symbol_policy=policy.universe[0],
        snapshot=snapshot,
        snapshot_map={},
        system=None,
        adapter=None,
        counters=counters,
        notes=notes,
    )

    signals = await repo.list_signals()
    assert [row.state for row in signals] == [SignalState.EXPIRED.value]
    assert counters["errors"] == 0
    assert await orchestrator._jev_pace_wait("BTCUSDT", NOW) == 0.0


async def test_a_candle_already_evaluated_is_not_asked_again(
    repo, server_limits, settings
):
    """A restart inside one candle must not pay for the same answer twice."""

    from conftest import market_snapshot as snap

    policy = compiled_policy(server_limits)
    snapshot = snap(NOW)
    await repo.insert_market_snapshot(snapshot)
    await repo.create_signal(
        symbol="BTCUSDT",
        policy_id=str(policy.policy_id),
        snapshot_id=str(snapshot.snapshot_id),
        side="buy",
        created_at=NOW,
    )

    class NoCandles:
        async def klines(self, *_args, **_kwargs):
            return []

    class NoJev:
        async def evaluate(self, *_args, **_kwargs):
            raise AssertionError("Jev must not be asked about the same candle twice")

    orchestrator = TradingOrchestrator(
        settings=settings,
        repo=repo,
        policy_service=None,
        market=NoCandles(),
        jev=NoJev(),
        policy_generator=None,
        risk_engine=None,
        execution_for_mode={},
        modes=None,
        audit=FakeAudit(),
        broker=FakeBroker(),
        account_id="a1",
    )
    counters = {"entries_blocked": 0, "signals_created": 0, "errors": 0}
    notes: list[str] = []

    await orchestrator._process_entry(
        mode=Mode.PAPER,
        policy=policy,
        policy_row=None,
        symbol_policy=policy.universe[0],
        snapshot=snapshot,
        snapshot_map={},
        system=None,
        adapter=None,
        counters=counters,
        notes=notes,
    )

    assert counters["signals_created"] == 0
    assert counters["entries_blocked"] == 1
    assert len(await repo.list_signals()) == 1
    assert "already evaluated" in notes[0]
