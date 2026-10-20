"""The cycle fails closed when a paid budget is spent, before any signal row."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

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
        return [self.snapshot.model_copy(update={'snapshot_id': uuid4()})]

    async def klines(self, symbol, interval="1m", limit=30, warmup=0):
        return []


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


def test_one_entry_spends_a_configured_share_of_cash():
    from types import SimpleNamespace

    from jev_trading.config import Settings
    from jev_trading.services.orchestrator import entry_notional

    account = SimpleNamespace(available_quote_usdt=Decimal("13.89"))
    cap = SimpleNamespace(max_position_notional_usdt=Decimal("6"))
    rules = SimpleNamespace(min_notional=Decimal("5"))

    # 13.89 * 0.25 = 3.47 < 5: nothing can be sized, so the cycle must say so.
    assert entry_notional(
        Settings(entry_budget_fraction=Decimal("0.25")), cap, account, rules
    ) == Decimal("0")
    # 13.89 * 0.40 = 5.556: fundable, and inside the 6 USDT policy cap.
    assert entry_notional(
        Settings(entry_budget_fraction=Decimal("0.40")), cap, account, rules
    ) == Decimal("5.556")
    # The policy's per-symbol cap still wins when it is the tighter ceiling.
    assert entry_notional(
        Settings(entry_budget_fraction=Decimal("1")),
        SimpleNamespace(max_position_notional_usdt=Decimal("5.2")),
        account,
        rules,
    ) == Decimal("5.2")


class _CountingJev:
    """Answers one BUY that passes the test policy's gate."""

    model_name = "test/jev"
    calls = 0

    async def evaluate(self, state, questions, signal_id=None):
        type(self).calls += 1
        return SimpleNamespace(
            model=self.model_name,
            raw={"usage": {"input_tokens": 10, "output_tokens": 1}},
            entry_choice="buy",
            entry_probability=0.7,
            policy_fit=SimpleNamespace(noul=0.8, model_dump=lambda **_: {"noul": 0.8}),
            entry_quality=SimpleNamespace(score=2.5, model_dump=lambda **_: {"score": 2.5}),
            entry_action=SimpleNamespace(model_dump=lambda **_: {}),
            latency_ms=5,
            simulated=True,
        )


async def test_a_second_tick_on_the_same_candle_calls_jev_once(tmp_path, monkeypatch):
    """One cycle per closed candle, whatever cadence drives the cycles."""

    monkeypatch.setattr(orchestrator_module, "datetime", _FrozenDatetime)
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'candle.db'}")
    await db.create_all()
    repo = Repository(db)

    settings = Settings(jev_min_call_interval_seconds=3600)
    policy = compiled_policy(settings.server_limits)
    ctx = make_context(policy)
    account = await repo.get_or_create_account("main", Mode.PAPER, Decimal("10000"))
    audit = _Audit()
    jev = _CountingJev()
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
        jev=jev,
        policy_generator=None,
        risk_engine=None,
        execution_for_mode={Mode.PAPER: _HealthyAdapter()},
        modes=_Modes(),
        audit=audit,
        broker=_Broker(),
        account_id=account.id,
        budgets=None,
    )
    _CountingJev.calls = 0
    first = await orchestrator.run_cycle()
    assert first.entries_blocked_reason is None
    assert _CountingJev.calls == 1

    second = await orchestrator.run_cycle()
    assert second.entries_blocked_reason == "no_new_candle"
    assert second.signals_created == 0
    assert _CountingJev.calls == 1
    assert any(event == "cycle.no_new_candle" for event, _ in audit.records)


async def test_jev_is_paced_per_symbol_between_closed_candles(tmp_path):
    """A fresh candle does not re-ask Jev while the pacing clock is running."""

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'pace.db'}")
    await db.create_all()
    repo = Repository(db)
    settings = Settings(jev_min_call_interval_seconds=3600, cycle_requires_new_candle=False)
    policy = compiled_policy(settings.server_limits)
    ctx = make_context(policy)
    account = await repo.get_or_create_account("main", Mode.PAPER, Decimal("10000"))
    jev = _CountingJev()
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
        jev=jev,
        policy_generator=None,
        risk_engine=None,
        execution_for_mode={Mode.PAPER: _HealthyAdapter()},
        modes=_Modes(),
        audit=_Audit(),
        broker=_Broker(),
        account_id=account.id,
        budgets=None,
    )
    # The symbol reached the market a minute ago: the pacing clock is running.
    from jev_trading.domain.enums import SignalState
    from jev_trading.services.orchestrator import MARKET_REACHED_SIGNAL_STATES

    signal = await repo.create_signal(
        symbol="BTCUSDT",
        policy_id=str(policy.policy_id),
        snapshot_id=str(ctx.market.snapshot_id),
        side="BUY",
        detail={},
    )
    for state in (
        SignalState.JEV_EVALUATING,
        SignalState.SCORED,
        SignalState.ELIGIBLE,
    ):
        await repo.transition_signal(signal.id, state)
    reached = await repo.last_market_reached_at("BTCUSDT", MARKET_REACHED_SIGNAL_STATES)
    assert reached is not None

    from datetime import timedelta

    wait = await orchestrator._jev_pace_wait(
        "BTCUSDT", reached + timedelta(seconds=60)
    )
    assert 3000 < wait <= 3600
    assert (
        await orchestrator._jev_pace_wait(
            "BTCUSDT", reached + timedelta(seconds=3600)
        )
        == 0
    )
    # A symbol that never reached the market has nothing to pace against.
    assert (
        await orchestrator._jev_pace_wait(
            "ETHUSDT", reached + timedelta(seconds=60)
        )
        == 0
    )
