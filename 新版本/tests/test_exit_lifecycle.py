"""Exit and dispatch lifecycle regressions.

Covers the failure modes found in review:

* a resting close order must not park its position in EXIT_PENDING forever;
* exits must be routed by the mode that opened the position, not by the
  currently selected mode (live resets to paper on restart);
* a dispatch must not report EXECUTED when the receipt left the signal in
  reconciliation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from conftest import NOW, compiled_policy, make_context, make_intent, system_snapshot
from jev_trading.adapters.execution.base import ExecutionReceipt, ReconciliationResult
from jev_trading.adapters.execution.paper import PaperExecutionAdapter
from jev_trading.domain.enums import (
    IntentType,
    Mode,
    OrderStatus,
    Side,
    SignalState,
)
from jev_trading.domain.money import ZERO
from jev_trading.domain.risk import RiskEngine
from jev_trading.services import orchestrator as orchestrator_module
from jev_trading.services.orchestrator import TradingOrchestrator
from jev_trading.storage.db import Database
from jev_trading.storage.models import PositionRow
from jev_trading.storage import repository as repository_module
from jev_trading.storage.repository import Repository


class ActiveContainer:
    """Minimal stand-in for AppContainer: selectable mode, route and kill state."""

    def __init__(self, mode: Mode = Mode.PAPER, *, kill: bool = False) -> None:
        self.mode = mode
        self.kill = kill
        self.account_id = ""
        self.orchestrator = None
        self.repo = None
        self.execution_for_mode: dict = {}
        self.audit = SimpleNamespace(record=_noop)
        self.broker = SimpleNamespace(publish=_noop)

    async def current_mode(self):
        return self.mode

    async def kill_switch_active(self):
        return self.kill


async def _noop(*args, **kwargs):
    return None


class _FrozenDatetime(datetime):
    """Pins wall-clock time to the fixtures' NOW."""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.fixture
async def repo(tmp_path, monkeypatch):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'exit.db'}")
    await db.create_all()
    # Services read the wall clock; the fixtures build every timestamp at NOW.
    monkeypatch.setattr(orchestrator_module, "datetime", _FrozenDatetime)
    monkeypatch.setattr(repository_module, "utcnow", lambda: NOW)
    try:
        yield Repository(db)
    finally:
        await db.dispose()


class Stub:
    """Execution adapter that records routing and returns a fixed receipt."""

    name = "stub"

    def __init__(self, receipt: ExecutionReceipt | None = None) -> None:
        self.receipt = receipt
        self.submitted: list = []
        self.cancelled: list[str] = []

    async def submit(self, approved, market, rules):
        self.submitted.append(approved)
        return self.receipt or ExecutionReceipt(order_id=str(uuid4()), status=OrderStatus.ACCEPTED)

    async def cancel(self, order_id: str) -> None:
        self.cancelled.append(order_id)

    async def health(self):
        return {"healthy": True}

    async def reconcile(self, account_id: str):
        return ReconciliationResult(healthy=True)

    async def close(self) -> None:
        return None


def _awaitable(value):
    async def _run():
        return value

    return _run()


def make_orchestrator(
    repo: Repository, container: ActiveContainer, account_id: str
) -> TradingOrchestrator:
    orchestrator = TradingOrchestrator(
        settings=SimpleNamespace(
            market_staleness_ms=5000,
            exit_slippage_bps=Decimal("8"),
            allowed_symbols=["BTCUSDT", "ETHUSDT"],
        ),
        repo=repo,
        policy_service=None,
        market=None,
        jev=None,
        policy_generator=None,
        risk_engine=RiskEngine(),
        execution_for_mode=container.execution_for_mode,
        modes=container,
        audit=container.audit,
        broker=container.broker,
        account_id=account_id,
    )
    async def _now():
        return NOW

    orchestrator._now = _now
    container.account_id = account_id
    container.orchestrator = orchestrator
    return orchestrator




class ChainedMarket:
    """Market service double: the first filter query must raise AttributeError."""

    def __init__(self, rules_queue: list) -> None:
        self.rules_queue = list(rules_queue)
        self.calls = 0

    async def filters(self, symbol):
        return self.rules_queue.pop(0)


async def open_position(repo, server_limits):
    """Create an account, a filled paper entry, and the matching signal."""

    account = await repo.get_or_create_account("main", Mode.PAPER, Decimal("10000"))
    policy = compiled_policy(server_limits, symbols=("BTCUSDT", "ETHUSDT"))
    ctx = make_context(policy)
    engine = RiskEngine()
    signal = await repo.create_signal(
        symbol="BTCUSDT",
        policy_id=str(policy.policy_id),
        snapshot_id=str(ctx.market.snapshot_id),
        side="BUY",
    )
    intent = make_intent(policy, ctx, account_id=account.id, signal_id=signal.id)
    decision = engine.evaluate(intent, ctx)
    assert decision.approved
    await repo.insert_intent(intent, status="EXECUTION_PENDING")
    await repo.insert_risk_decision(decision)
    adapter = PaperExecutionAdapter(repo, seed=7)
    receipt = await adapter.submit(decision.approved_intent, ctx.market, ctx.symbol_rules)
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
    position = (await repo.get_open_positions(account.id))[0]
    return account, policy, ctx, engine, signal, position, adapter


async def park_resting_close(repo, policy, ctx, position, engine, idempotency):
    """Park a close that is still working, as a resting exchange order would be.

    The paper adapter fills or leaves nothing behind, so the ledger state a
    working close leaves - an ACCEPTED order, the position still open, the
    signal in EXIT_PENDING - is written directly.
    """

    market = ctx.market.model_copy(
        update={"bid": ctx.market.bid + 50, "ask": ctx.market.ask - 10}
    )
    close_ctx = make_context(policy, market=market, position_qty=str(position.qty))
    close_intent = make_intent(
        policy,
        close_ctx,
        intent_type=IntentType.CLOSE,
        side=Side.SELL,
        quote_notional=str(position.qty * market.bid),
        limit_price=str(close_ctx.market.ask),
        account_id=position.account_id,
        signal_id=position.signal_id,
        idempotency=idempotency,
    )
    close_decision = engine.evaluate(close_intent, close_ctx)
    assert close_decision.approved, close_decision.failed_rule_names
    await repo.insert_intent(close_intent, status="EXECUTION_PENDING")
    await repo.insert_risk_decision(close_decision)
    await repo.record_order_row(
        approved=close_decision.approved_intent,
        order_id=str(uuid4()),
        status=OrderStatus.ACCEPTED,
        now=NOW,
    )
    await repo.update_intent_status(str(close_intent.intent_id), "ACCEPTED")
    await repo.transition_signal(str(position.signal_id), SignalState.EXIT_PENDING)
    return market


async def test_stale_resting_close_is_cancelled_and_position_reopens(
    repo, server_limits, monkeypatch
):
    account, policy, ctx, engine, signal, position, paper = await open_position(
        repo, server_limits
    )
    market = await park_resting_close(
        repo, policy, ctx, position, engine, "close-lifecycle-1"
    )

    monkeypatch.setattr(orchestrator_module, "CLOSE_ORDER_WORKING_LIMIT_SECONDS", 0)
    container = ActiveContainer()
    container.execution_for_mode = {Mode.PAPER: paper}
    orchestrator = make_orchestrator(repo, container, account.id)
    exits, notes = await orchestrator._manage_exits(
        policy=policy, snapshot_map={market.symbol: market}, system=system_snapshot()
    )

    assert exits == 0, notes
    assert (await repo.get_signal(signal.id)).state == SignalState.POSITION_OPEN.value, notes
    assert any("cancelled" in note for note in notes), notes


async def test_working_close_is_left_alone_until_it_times_out(repo, server_limits):
    account, policy, ctx, engine, signal, position, paper = await open_position(
        repo, server_limits
    )
    market = await park_resting_close(
        repo, policy, ctx, position, engine, "close-lifecycle-2"
    )

    container = ActiveContainer()
    container.execution_for_mode = {Mode.PAPER: paper}
    orchestrator = make_orchestrator(repo, container, account.id)
    exits, notes = await orchestrator._manage_exits(
        policy=policy, snapshot_map={market.symbol: market}, system=system_snapshot()
    )
    assert exits == 0, notes
    assert (await repo.get_signal(signal.id)).state == SignalState.EXIT_PENDING.value, notes
    assert any("working" in note for note in notes), notes


async def test_fresh_exit_uses_a_retry_key_when_an_older_close_still_works(
    repo, server_limits
):
    account, policy, ctx, engine, signal, position, paper = await open_position(
        repo, server_limits
    )
    # An older close for this symbol is still ACCEPTED (for another signal).
    await park_resting_close(repo, policy, ctx, position, engine, "close-earlier-1")

    other = await repo.create_signal(
        symbol="BTCUSDT",
        policy_id=str(policy.policy_id),
        snapshot_id=str(ctx.market.snapshot_id),
        side="BUY",
    )
    for state in (
        SignalState.JEV_EVALUATING,
        SignalState.SCORED,
        SignalState.ELIGIBLE,
        SignalState.INTENT_CREATED,
        SignalState.RISK_PENDING,
        SignalState.APPROVED,
        SignalState.EXECUTION_PENDING,
    ):
        await repo.transition_signal(other.id, state)
    # A second, separate open position for the same symbol carrying its own signal.
    async with repo.db.session() as session:
        row = await session.get(PositionRow, position.id)
        row.status = "CLOSED"
        session.add(
            PositionRow(
                account_id=account.id,
                symbol="BTCUSDT",
                qty=position.qty,
                avg_entry_price=position.avg_entry_price,
                stop_loss_bps=Decimal("1"),
                take_profit_bps=Decimal("200"),
                max_holding_seconds=3600,
                status="OPEN",
                opened_at=NOW,
                policy_id=str(policy.policy_id),
                signal_id=other.id,
                mode=Mode.PAPER.value,
                is_simulated=True,
            )
        )
    await repo.transition_signal(other.id, SignalState.ACCEPTED)
    await repo.transition_signal(other.id, SignalState.FILLED)
    await repo.transition_signal(other.id, SignalState.POSITION_OPEN)

    container = ActiveContainer()
    container.execution_for_mode = {Mode.PAPER: paper}
    orchestrator = make_orchestrator(repo, container, account.id)
    orchestrator.market = ChainedMarket([ctx.symbol_rules])
    snapshot = ctx.market.model_copy(
        update={"bid": ctx.market.bid * Decimal("0.98"), "ask": ctx.market.ask * Decimal("0.98")}
    )
    await orchestrator._manage_exits(
        policy=policy, snapshot_map={snapshot.symbol: snapshot}, system=system_snapshot()
    )

    closes = [
        intent
        for intent in await repo.list_intents(limit=20)
        if intent.intent_type == "CLOSE" and intent.signal_id == other.id
    ]
    assert len(closes) == 1, closes
    assert "retry-" in closes[0].idempotency_key


async def test_live_position_exits_through_live_route_after_restart_to_paper(
    repo, server_limits
):
    account, policy, ctx, engine, signal, position, _paper = await open_position(
        repo, server_limits
    )
    # Position was opened on the live route; a restart reset the mode to paper.
    async with repo.db.session() as session:
        row = await session.get(PositionRow, position.id)
        row.mode = Mode.LIVE.value
        row.stop_loss_bps = Decimal("50")  # a 0.5% stop that the mark will hit
        row.opened_at = NOW

    live = Stub()
    paper_route = Stub()
    container = ActiveContainer()
    container.execution_for_mode = {Mode.LIVE: live, Mode.PAPER: paper_route}
    orchestrator = make_orchestrator(repo, container, account.id)
    orchestrator.market = ChainedMarket([ctx.symbol_rules])
    snapshot = ctx.market.model_copy(
        update={"bid": ctx.market.bid * Decimal("0.98"), "ask": ctx.market.ask * Decimal("0.98")}
    )
    await orchestrator._manage_exits(
        policy=policy, snapshot_map={snapshot.symbol: snapshot}, system=system_snapshot()
    )

    assert len(live.submitted) == 1, live.submitted
    assert live.submitted[0].intent.mode is Mode.LIVE
    assert paper_route.submitted == []


async def test_kill_switch_defers_cross_route_exit_but_keeps_local_exits(
    repo, server_limits
):
    account, policy, ctx, engine, signal, position, _paper = await open_position(
        repo, server_limits
    )
    async with repo.db.session() as session:
        row = await session.get(PositionRow, position.id)
        row.mode = Mode.LIVE.value
        row.stop_loss_bps = Decimal("50")
        row.opened_at = NOW

    live = Stub()
    container = ActiveContainer(kill=True)
    container.execution_for_mode = {Mode.LIVE: live}
    orchestrator = make_orchestrator(repo, container, account.id)
    orchestrator.market = ChainedMarket([ctx.symbol_rules])
    snapshot = ctx.market.model_copy(
        update={"bid": ctx.market.bid * Decimal("0.98"), "ask": ctx.market.ask * Decimal("0.98")}
    )
    _exits, notes = await orchestrator._manage_exits(
        policy=policy, snapshot_map={snapshot.symbol: snapshot}, system=system_snapshot()
    )
    assert live.submitted == []
    assert any("kill switch" in note for note in notes)


async def test_same_route_exits_still_run_under_the_kill_switch(repo, server_limits):
    account, policy, ctx, engine, signal, position, paper = await open_position(
        repo, server_limits
    )
    async with repo.db.session() as session:
        row = await session.get(PositionRow, position.id)
        row.stop_loss_bps = Decimal("50")
        row.opened_at = NOW

    container = ActiveContainer(kill=True)
    container.execution_for_mode = {Mode.PAPER: paper}
    orchestrator = make_orchestrator(repo, container, account.id)
    orchestrator.market = ChainedMarket([ctx.symbol_rules])
    snapshot = ctx.market.model_copy(
        update={"bid": ctx.market.bid * Decimal("0.98"), "ask": ctx.market.ask * Decimal("0.98")}
    )
    await orchestrator._manage_exits(
        policy=policy, snapshot_map={snapshot.symbol: snapshot}, system=system_snapshot()
    )
    assert (await repo.get_signal(signal.id)).state == SignalState.CLOSED.value


async def test_dispatch_marks_unknown_receipt_as_reconciliation(repo, server_limits):
    account, policy, _ctx, engine, _signal, _position, _paper = await open_position(
        repo, server_limits
    )
    eth_ctx = make_context(policy)
    signal = await repo.create_signal(
        symbol="ETHUSDT",
        policy_id=str(policy.policy_id),
        snapshot_id=str(eth_ctx.market.snapshot_id),
        side="BUY",
    )
    intent = make_intent(
        policy, eth_ctx, account_id=account.id, symbol="ETHUSDT", signal_id=signal.id,
        idempotency="dispatch-unknown-1",
        created_at=NOW,
    )
    decision = engine.evaluate(intent, eth_ctx)
    assert decision.approved, decision.failed_rule_names
    await repo.insert_intent(intent, status="EXECUTION_PENDING")
    await repo.insert_risk_decision(decision)
    for state in (
        SignalState.JEV_EVALUATING,
        SignalState.SCORED,
        SignalState.ELIGIBLE,
        SignalState.INTENT_CREATED,
        SignalState.RISK_PENDING,
        SignalState.APPROVED,
    ):
        await repo.transition_signal(signal.id, state)
    await repo.transition_signal(signal.id, SignalState.EXECUTION_PENDING)

    unknown = Stub(ExecutionReceipt(order_id=str(uuid4()), status=OrderStatus.UNKNOWN))
    container = ActiveContainer()
    container.execution_for_mode = {Mode.PAPER: unknown}
    orchestrator = make_orchestrator(repo, container, account.id)
    orchestrator.market = SimpleNamespace(
        snapshots=lambda symbols, now=None: _awaitable([eth_ctx.market]),
        filters=lambda symbol: _awaitable(eth_ctx.symbol_rules),
    )
    result = await orchestrator.dispatch_approved_intent(str(intent.intent_id))

    assert result["status"] == SignalState.RECONCILIATION_REQUIRED.value
    assert (await repo.get_intent(str(intent.intent_id))).status == "RECONCILIATION_REQUIRED"
    assert (await repo.get_signal(signal.id)).state == (
        SignalState.RECONCILIATION_REQUIRED.value
    )


async def test_manual_close_exits_without_a_stop_being_hit(repo, server_limits):
    account, policy, ctx, engine, signal, position, paper = await open_position(
        repo, server_limits
    )
    async with repo.db.session() as session:
        row = await session.get(PositionRow, position.id)
        row.opened_at = NOW  # well inside the holding limit

    container = ActiveContainer()
    container.execution_for_mode = {Mode.PAPER: paper}
    orchestrator = make_orchestrator(repo, container, account.id)
    orchestrator.market = ChainedMarket([ctx.symbol_rules])

    stamped = await orchestrator.request_close(position.id, actor="operator:test")
    assert stamped.exit_requested_reason == "manual"
    assert stamped.exit_requested_at is not None

    snapshot = ctx.market.model_copy(
        update={
            "bid": ctx.market.bid * Decimal("0.999"),
            "ask": ctx.market.ask * Decimal("0.999"),
        }
    )
    exits, _notes = await orchestrator._manage_exits(
        policy=policy, snapshot_map={snapshot.symbol: snapshot}, system=system_snapshot()
    )
    assert exits == 1
    assert (await repo.get_signal(signal.id)).state == SignalState.CLOSED.value
    assert await repo.get_open_positions(account.id) == []


async def test_manual_close_request_on_a_closed_position_is_not_found(repo, server_limits):
    from jev_trading.domain.errors import AppError

    account, _policy, _ctx, _engine, _signal, position, _paper = await open_position(
        repo, server_limits
    )
    container = ActiveContainer()
    orchestrator = make_orchestrator(repo, container, account.id)
    assert await repo.get_open_positions(account.id)
    async with repo.db.session() as session:
        row = await session.get(PositionRow, position.id)
        row.status = "CLOSED"
    with pytest.raises(AppError) as raised:
        await orchestrator.request_close(position.id, actor="operator:test")
    assert raised.value.http_status == 404
