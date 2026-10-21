"""Exit and dispatch lifecycle regressions.

Covers the failure modes found in review:

* a resting close order must not park its position in EXIT_PENDING forever;
* exits must be routed by the mode that opened the position, not by the
  currently selected mode (live resets to paper on restart);
* a dispatch must not report EXECUTED when the receipt left the signal in
  reconciliation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
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
from jev_trading.domain.risk import RiskEngine
from jev_trading.services import orchestrator as orchestrator_module
from jev_trading.services.orchestrator import TradingOrchestrator
from jev_trading.storage import models as models_module
from jev_trading.storage import repository as repository_module
from jev_trading.storage.db import Database
from jev_trading.storage.models import PositionRow
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

    async def is_live_armed(self):
        return False

    async def system_snapshot(self, *, feed_healthy, execution_healthy, policy_fresh):
        return system_snapshot(
            mode=self.mode,
            kill=self.kill,
            feed=feed_healthy,
            execution=execution_healthy,
        )


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
    # Row defaults come from the models module, so it needs the same clock or
    # a freshly inserted row lands hours away from the fixtures' NOW.
    monkeypatch.setattr(models_module, "utcnow", lambda: NOW)
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
            exit_stress_drawdown_bps=Decimal("300"),
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
    # Dispatch re-checks risk against the live policy, so it has to be stored.
    await repo.insert_policy(policy, source="test", is_simulated=True)
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
        health=lambda: _awaitable({"healthy": True}),
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


# ---------------------------------------------------------------------------
# resting sell re-pricing
# ---------------------------------------------------------------------------
def _resting_sell_market(ctx):
    """A market where the bid fell well below the resting close's limit."""

    # The sold instrument is a sub-dollar-ish pair in the fixtures' terms, so
    # the step below the bid is rounded to the ledger's own precision.
    resting_limit = ctx.market.ask
    bid = (resting_limit * Decimal("0.95")).quantize(Decimal("0.0001"))
    return ctx.market.model_copy(update={"bid": bid, "ask": bid})


async def test_resting_sell_far_from_the_bid_is_cancelled_and_repriced_within_the_cycle(
    repo, server_limits
):
    account, policy, ctx, engine, signal, position, paper = await open_position(
        repo, server_limits
    )
    await park_resting_close(repo, policy, ctx, position, engine, "close-stale-1")
    market = _resting_sell_market(ctx)

    container = ActiveContainer()
    container.execution_for_mode = {Mode.PAPER: paper}
    orchestrator = make_orchestrator(repo, container, account.id)
    _age_the_clock(orchestrator)
    orchestrator.market = ChainedMarket([ctx.symbol_rules] * 4)
    exits, notes = await orchestrator._manage_exits(
        policy=policy, snapshot_map={market.symbol: market}, system=system_snapshot()
    )

    # The bid is below the stop, so the cycle both re-prices and closes.
    assert exits == 1, notes
    assert any("re-priced" in note for note in notes), notes
    assert (await repo.get_signal(signal.id)).state == SignalState.CLOSED.value
    closes = [
        intent
        for intent in await repo.list_intents(limit=20)
        if intent.intent_type == "CLOSE"
    ]
    assert len(closes) == 2, "a second close must be generated after the cancel"
    replacement = [item for item in closes if item.idempotency_key != "close-stale-1"]
    assert len(replacement) == 1
    # The replacement chases the bid; the cancelled order was parked above it.
    assert replacement[0].limit_price < market.bid


async def test_a_resting_sell_near_the_bid_is_left_working(repo, server_limits):
    account, policy, ctx, engine, signal, position, paper = await open_position(
        repo, server_limits
    )
    await park_resting_close(repo, policy, ctx, position, engine, "close-fresh-1")
    market = ctx.market.model_copy(update={"bid": ctx.market.ask - Decimal("0.01")})

    container = ActiveContainer()
    container.execution_for_mode = {Mode.PAPER: paper}
    orchestrator = make_orchestrator(repo, container, account.id)
    _age_the_clock(orchestrator)
    orchestrator.market = ChainedMarket([ctx.symbol_rules] * 4)
    exits, notes = await orchestrator._manage_exits(
        policy=policy, snapshot_map={market.symbol: market}, system=system_snapshot()
    )
    assert exits == 0, notes
    assert (await repo.get_signal(signal.id)).state == SignalState.EXIT_PENDING.value
    assert any("working" in note for note in notes), notes


async def test_a_fresh_reprice_waits_for_the_next_candle(repo, server_limits):
    account, policy, ctx, engine, signal, position, paper = await open_position(
        repo, server_limits
    )
    await park_resting_close(repo, policy, ctx, position, engine, "close-lag-1")
    market = _resting_sell_market(ctx)

    container = ActiveContainer()
    container.execution_for_mode = {Mode.PAPER: paper}
    orchestrator = make_orchestrator(repo, container, account.id)
    _age_the_clock(orchestrator)
    orchestrator.market = ChainedMarket([ctx.symbol_rules] * 4)
    # As if this same candle's bid had already been chased once.
    orchestrator._reprice_guard[position.id] = (
        market.symbol,
        int(market.candle_close_at.timestamp()),
    )

    exits, notes = await orchestrator._manage_exits(
        policy=policy, snapshot_map={market.symbol: market}, system=system_snapshot()
    )
    assert exits == 0, notes
    assert any("deferred to the next candle" in note for note in notes), notes
    assert (await repo.get_signal(signal.id)).state == SignalState.EXIT_PENDING.value


async def test_a_replacement_close_never_collides_with_the_cancelled_key(
    repo, server_limits
):
    """The cancelled order shares this minute's key; the replacement must not.

    The guard from the re-price is exercised by the test above; here it is
    primed with another snapshot so the fresh close is generated at once.
    """

    account, policy, ctx, engine, signal, position, paper = await open_position(
        repo, server_limits
    )
    await park_resting_close(repo, policy, ctx, position, engine, "close-stale-2")
    market = _resting_sell_market(ctx)

    container = ActiveContainer()
    container.execution_for_mode = {Mode.PAPER: paper}
    orchestrator = make_orchestrator(repo, container, account.id)
    _age_the_clock(orchestrator)
    orchestrator.market = ChainedMarket([ctx.symbol_rules] * 4)
    # Primed with an unrelated snapshot so the re-price is not deferred.
    orchestrator._reprice_guard[position.id] = (market.symbol, 0)

    exits, notes = await orchestrator._manage_exits(
        policy=policy, snapshot_map={market.symbol: market}, system=system_snapshot()
    )
    assert exits == 1, notes
    assert any("re-priced" in note for note in notes), notes
    # The venue keeps the cancelled order, so this cycle also uses a retry
    # key; what matters is that the replacement never reuses the stale key.
    intents = [
        intent
        for intent in await repo.list_intents(limit=20)
        if intent.intent_type == "CLOSE"
    ]
    replacement = [item for item in intents if item.idempotency_key != "close-stale-2"]
    assert len(replacement) == 1, [item.idempotency_key for item in intents]
    assert "close-stale-2" not in replacement[0].idempotency_key
    # Keyed on the candle that was chased, never on the wall-clock minute.
    chased = int(market.candle_close_at.timestamp())
    assert replacement[0].idempotency_key.endswith(f"reprice-{chased}-sell")


def _age_the_clock(orchestrator, seconds: int = 90) -> None:
    """Pretend the close has been working for longer than the re-price delay."""

    async def _later():
        return NOW + timedelta(seconds=seconds)

    orchestrator._now = _later


# ---------------------------------------------------------------------------
# over-trading protection
# ---------------------------------------------------------------------------
def _entry_signal(
    repo,
    policy,
    ctx,
    symbol: str,
    at: datetime,
    state: SignalState = SignalState.ELIGIBLE,
):
    """An entry signal that reached the market, backdated to ``at``.

    Column defaults bind the clock at import time, so the timestamp is set
    explicitly rather than monkeypatched.
    """

    return repo.create_signal(
        symbol=symbol,
        policy_id=str(policy.policy_id),
        snapshot_id=str(ctx.market.snapshot_id),
        side="BUY",
        state=state,
        created_at=at,
    )


def _guard_orchestrator(repo, account, paper, *, window: int, maximum: int):
    container = ActiveContainer()
    container.execution_for_mode = {Mode.PAPER: paper}
    orchestrator = make_orchestrator(repo, container, account.id)
    orchestrator.settings.overtrade_window_seconds = window
    orchestrator.settings.overtrade_max_entries = maximum
    return orchestrator


async def test_overtrade_guard_blocks_once_the_window_is_full(repo, server_limits):
    account, policy, ctx, engine, _signal, _position, paper = await open_position(
        repo, server_limits
    )
    orchestrator = _guard_orchestrator(repo, account, paper, window=3600, maximum=3)
    # Signals Jev never let through are still evaluated, and must not count.
    for minutes in (5, 6):
        await _entry_signal(
            repo, policy, ctx, "ETHUSDT", NOW - timedelta(minutes=minutes), SignalState.REJECTED
        )
    assert await orchestrator._overtrade_wait("ETHUSDT", NOW) == 0.0

    for minutes in (10, 20):
        await _entry_signal(repo, policy, ctx, "ETHUSDT", NOW - timedelta(minutes=minutes))
    assert await orchestrator._overtrade_wait("ETHUSDT", NOW) == 0.0
    await _entry_signal(repo, policy, ctx, "ETHUSDT", NOW - timedelta(minutes=1))

    wait = await orchestrator._overtrade_wait("ETHUSDT", NOW)
    # The third-newest signal (20 minutes ago) leaves 40 minutes of the hour.
    assert 39 * 60 < wait <= 40 * 60, wait
    # Another symbol is unaffected by this symbol's history.
    assert await orchestrator._overtrade_wait("BTCUSDT", NOW) == 0.0


async def test_overtrade_guard_ignores_signals_older_than_the_window(repo, server_limits):
    account, policy, ctx, engine, _signal, _position, paper = await open_position(
        repo, server_limits
    )
    orchestrator = _guard_orchestrator(repo, account, paper, window=600, maximum=2)
    for minutes in (30, 25):
        await _entry_signal(repo, policy, ctx, "ETHUSDT", NOW - timedelta(minutes=minutes))

    assert await orchestrator._overtrade_wait("ETHUSDT", NOW) == 0.0


async def test_overtrade_guard_can_be_switched_off(repo, server_limits):
    account, policy, ctx, engine, _signal, _position, paper = await open_position(
        repo, server_limits
    )
    orchestrator = _guard_orchestrator(repo, account, paper, window=0, maximum=1)
    for minutes in (1, 2, 3):
        await _entry_signal(repo, policy, ctx, "ETHUSDT", NOW - timedelta(minutes=minutes))

    assert await orchestrator._overtrade_wait("ETHUSDT", NOW) == 0.0


async def test_dispatch_refuses_a_price_the_risk_engine_never_approved(
    repo, server_limits
):
    """A spread that blew out after approval must stop the order, not fill it."""

    account, policy, _ctx, engine, _signal, _position, _paper = await open_position(
        repo, server_limits
    )
    await repo.insert_policy(policy, source="test", is_simulated=True)

    eth_ctx = make_context(policy)
    signal = await repo.create_signal(
        symbol="ETHUSDT",
        policy_id=str(policy.policy_id),
        snapshot_id=str(eth_ctx.market.snapshot_id),
        side="BUY",
    )
    intent = make_intent(
        policy, eth_ctx, account_id=account.id, symbol="ETHUSDT", signal_id=signal.id,
        idempotency="recheck-spread-1",
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
        SignalState.EXECUTION_PENDING,
    ):
        await repo.transition_signal(signal.id, state)

    # The book widens far past the policy limit between approval and dispatch.
    widened = eth_ctx.market.model_copy(
        update={"bid": Decimal("3190"), "ask": Decimal("3210")}
    )
    submitted = Stub(ExecutionReceipt(order_id=str(uuid4()), status=OrderStatus.FILLED))
    container = ActiveContainer()
    container.execution_for_mode = {Mode.PAPER: submitted}
    orchestrator = make_orchestrator(repo, container, account.id)
    orchestrator.market = SimpleNamespace(
        snapshots=lambda symbols, now=None: _awaitable([widened]),
        filters=lambda symbol: _awaitable(eth_ctx.symbol_rules),
        health=lambda: _awaitable({"healthy": True}),
    )

    result = await orchestrator.dispatch_approved_intent(str(intent.intent_id))

    assert result["status"] == SignalState.ERROR.value
    assert submitted.submitted == []
    assert (await repo.get_intent(str(intent.intent_id))).status == "ERROR"
