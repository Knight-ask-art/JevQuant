"""The edge evaluator in a real cycle: it records, and it never blocks.

The evaluator's whole premise is that it is not trusted yet. These tests pin
that: with no history the signal must still become a trade, the verdict must be
on the record, and a failure inside the evaluator must not cost the trade.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from conftest import NOW, compiled_policy, make_context, system_snapshot
from jev_trading.adapters.models.base import (
    ChoiceAnswer,
    JevResult,
    NoulAnswer,
    ScoreAnswer,
)
from jev_trading.config import Settings
from jev_trading.domain.enums import Mode
from jev_trading.services import orchestrator as orchestrator_module
from jev_trading.storage.db import Database
from jev_trading.storage.models import EdgeShadowRow, SignalRow
from jev_trading.storage.repository import Repository


class _Market:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def snapshots(self, symbols, now=None):
        return [self.snapshot.model_copy(update={"snapshot_id": uuid4()})]

    async def klines(self, symbol, interval="1m", limit=30, warmup=0):
        return []

    async def filters(self, symbol):
        return make_context(compiled_policy(Settings().server_limits)).symbol_rules


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

    def events(self):
        return [event for event, _ in self.records]


class _Broker:
    async def publish(self, channel, payload):
        pass


class _HealthyAdapter:
    async def health(self):
        return {"healthy": True}


class _Jev:
    model_name = "test/jev"

    async def evaluate(self, state, questions, signal_id=None):
        return JevResult(
            model="test/jev",
            evaluation_id=str(uuid4()),
            entry_action=ChoiceAnswer(
                choice="BUY", confidence=0.9, probabilities={"buy": 0.9}
            ),
            entry_quality=ScoreAnswer(score=3.0),
            policy_fit=NoulAnswer(noul=0.9),
            latency_ms=1,
            raw={},
        )


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


async def _cycle(tmp_path, monkeypatch, *, evaluator=None, enabled=True):
    monkeypatch.setattr(orchestrator_module, "datetime", _FrozenDatetime)
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'edge.db'}")
    await db.create_all()
    repo = Repository(db)

    settings = Settings(edge_shadow_enabled=enabled)
    policy = compiled_policy(settings.server_limits)
    ctx = make_context(policy)
    account = await repo.get_or_create_account("main", Mode.PAPER, Decimal("10000"))
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
        jev=_Jev(),
        policy_generator=None,
        risk_engine=None,
        execution_for_mode={Mode.PAPER: _HealthyAdapter()},
        modes=_Modes(),
        audit=audit,
        broker=_Broker(),
        account_id=account.id,
        regime_gate=None,
        edge_evaluator=evaluator,
    )
    result = await orchestrator.run_cycle()
    async with repo.db.session() as session:
        signals = list((await session.execute(select(SignalRow))).scalars())
        shadows = list((await session.execute(select(EdgeShadowRow))).scalars())
    await db.dispose()
    return result, signals, shadows, audit


async def test_with_no_history_the_trade_still_happens_and_the_verdict_is_recorded(
    tmp_path, monkeypatch
):
    result, signals, shadows, audit = await _cycle(tmp_path, monkeypatch)

    assert len(signals) == 1
    assert signals[0].state != "REJECTED"
    assert len(shadows) == 1
    verdict = shadows[0]
    assert verdict.blocked is True
    assert verdict.reason == "insufficient_evidence"
    assert verdict.sample_size == 0
    # The cohort is on the record even when nothing could be said about it.
    assert verdict.cohort.startswith("BTCUSDT|")
    assert "edge.shadow_block" in audit.events()


async def test_an_evaluator_failure_is_audited_and_never_stops_the_trade(
    tmp_path, monkeypatch
):
    class _Broken:
        async def evaluate(self, **kwargs):
            raise RuntimeError("lookup exploded")

    result, signals, shadows, audit = await _cycle(tmp_path, monkeypatch, evaluator=_Broken())

    assert len(signals) == 1
    assert signals[0].state != "REJECTED"
    assert shadows == []
    assert "edge.shadow_error" in audit.events()


async def test_disabled_means_no_lookup_and_no_record(tmp_path, monkeypatch):
    result, signals, shadows, audit = await _cycle(
        tmp_path, monkeypatch, enabled=False
    )

    assert len(signals) == 1
    assert shadows == []
    assert "edge.shadow_block" not in audit.events()
