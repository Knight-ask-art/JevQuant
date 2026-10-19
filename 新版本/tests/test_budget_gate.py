"""The cycle fails closed when a paid budget is spent, before any signal row."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import update

from conftest import NOW, compiled_policy, make_context, system_snapshot
from jev_trading.config import Settings
from jev_trading.domain.enums import Mode
from jev_trading.services import orchestrator as orchestrator_module
from jev_trading.services.budgets import BudgetService
from jev_trading.storage.db import Database
from jev_trading.storage.models import JevEvaluationRow
from jev_trading.storage.repository import Repository


class _Market:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def snapshots(self, symbols, now=None):
        return [self.snapshot]


class _Modes:
    async def current_mode(self):
        return Mode.PAPER

    async def kill_switch_active(self):
        return False

    async def system_snapshot(self, **kwargs):
        return system_snapshot()


class _Audit:
    def __init__(self):
        self.records = []

    async def record(self, event, kind, ref, payload):
        self.records.append((event, payload))


class _Broker:
    async def publish(self, channel, payload):
        pass


class _HealthyAdapter:
    async def health(self):
        return {"healthy": True}


class _FrozenDatetime:
    @staticmethod
    def now(tz=None):
        return NOW

    def __new__(cls, *args, **kwargs):
        return datetime(*args, **kwargs)


def _awaitable(value):
    async def _run():
        return value

    return _run()


async def test_a_spent_jev_budget_blocks_entries_without_a_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator_module, "datetime", _FrozenDatetime)
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'cycle.db'}")
    await db.create_all()
    repo = Repository(db)

    settings = Settings(jev_max_calls_per_utc_day=1)
    policy = compiled_policy(settings.server_limits)
    ctx = make_context(policy)
    account = await repo.get_or_create_account("main", Mode.PAPER, Decimal("10000"))

    # One request already reached Jev today: the day's budget is spent.
    row = await repo.insert_jev_evaluation(
        signal_id=None,
        model="typesev/test",
        request={},
        response={"usage": {"input_tokens": 10}},
        answers={},
    )
    async with repo.db.session() as session:
        await session.execute(
            update(JevEvaluationRow).where(JevEvaluationRow.id == row.id).values(created_at=NOW)
        )

    audit = _Audit()
    orchestrator = orchestrator_module.TradingOrchestrator(
        settings=settings,
        repo=repo,
        policy_service=type(
            "Policy",
            (),
            {
                "get_active": lambda self, mode: _awaitable(policy),
                "get_active_row": lambda self: _awaitable(None),
            },
        )(),
        market=_Market(ctx.market),
        jev=None,
        policy_generator=None,
        risk_engine=None,
        execution_for_mode={Mode.PAPER: _HealthyAdapter()},
        modes=_Modes(),
        audit=audit,
        broker=_Broker(),
        account_id=account.id,
        budgets=BudgetService(repo, settings),
    )

    async def _now():
        return NOW

    orchestrator._now = _now
    result = await orchestrator.run_cycle()

    assert result.entries_blocked_reason == "jev_budget_exhausted"
    assert result.signals_created == 0
    assert any("budget" in note for note in result.notes)
    assert any(event == "cycle.budget_exhausted" for event, _ in audit.records)
    assert await repo.list_signals(limit=10) == []
