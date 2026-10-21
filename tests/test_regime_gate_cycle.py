"""The regime gate in the cycle: shadow records, enforcement refuses.

The gate's value depends on it being *measurable*, so these tests pin both
halves: with enforcement off the trade must still happen (and the verdict must
be on the record), and with it on the signal must be rejected before an intent
is ever built.
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
from jev_trading.storage.models import SignalRow
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
    """A Jev that always says buy, so the gate is what decides."""

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


async def _cycle(tmp_path, monkeypatch, *, atr_bps: float, enforce: bool):
    monkeypatch.setattr(orchestrator_module, "datetime", _FrozenDatetime)
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'regime.db'}")
    await db.create_all()
    repo = Repository(db)

    settings = Settings(regime_gate_enforce=enforce)
    policy = compiled_policy(settings.server_limits)
    ctx = make_context(policy)
    # A quiet market: a low ATR cannot cover the round trip over the horizon.
    snapshot = ctx.market.model_copy(
        update={"features": {**ctx.market.features, "atr_5m_bps": atr_bps}}
    )
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
        market=_Market(snapshot),
        jev=_Jev(),
        policy_generator=None,
        risk_engine=None,
        execution_for_mode={Mode.PAPER: _HealthyAdapter()},
        modes=_Modes(),
        audit=audit,
        broker=_Broker(),
        account_id=account.id,
    )
    result = await orchestrator.run_cycle()
    async with repo.db.session() as session:
        signals = list((await session.execute(select(SignalRow))).scalars())
    return result, signals, audit


async def test_shadow_mode_records_the_verdict_and_still_trades(
    tmp_path, monkeypatch
):
    result, signals, audit = await _cycle(
        tmp_path, monkeypatch, atr_bps=1.0, enforce=False
    )

    assert len(signals) == 1
    # The trade path was not blocked: the signal cleared the Jev gate.
    assert signals[0].state != "REJECTED"
    assert "regime.shadow_block" in audit.events()
    shadow = [payload for event, payload in audit.records if event == "regime.shadow_block"]
    assert shadow[0]["regime_blocked"] is True
    assert shadow[0]["regime_reason"] == "expected_move_below_cost"


async def test_enforcement_rejects_the_signal_before_an_intent_exists(
    tmp_path, monkeypatch
):
    result, signals, audit = await _cycle(
        tmp_path, monkeypatch, atr_bps=1.0, enforce=True
    )

    assert len(signals) == 1
    assert signals[0].state == "REJECTED"
    assert result.rejected == 1
    assert "regime.shadow_block" not in audit.events()
    rejected = [payload for event, payload in audit.records if event == "signal.rejected"]
    assert rejected[0]["stage"] == "regime_gate"
    assert rejected[0]["regime_blocked"] is True


async def test_a_volatile_market_passes_in_both_modes(tmp_path, monkeypatch):
    for enforce in (False, True):
        result, signals, audit = await _cycle(
            tmp_path / f"v{int(enforce)}",
            monkeypatch,
            atr_bps=200.0,
            enforce=enforce,
        )
        assert len(signals) == 1
        assert signals[0].state != "REJECTED"
        assert "regime.shadow_block" not in audit.events()
