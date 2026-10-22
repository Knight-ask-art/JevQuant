"""Exit geometry v2 through the real exit manager, not just the pure rule.

The pure rule is pinned in ``test_exits.py``. What these tests add is the part
that only shows up in the wiring: the peak is persisted and only ever rises, a
ratchet is recorded once per level, and a position opened before v2 keeps its
original geometry.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import system_snapshot

from jev_trading.domain.enums import Mode
from jev_trading.storage.repository import Repository

from test_exit_lifecycle import (  # noqa: F401 - fixtures are shared
    ActiveContainer,
    Stub,
    make_orchestrator,
    open_position,
    repo,
)


def market_at(ctx, *, bid: str):
    return ctx.market.model_copy(update={"bid": Decimal(bid), "ask": Decimal(bid)})


async def test_the_peak_is_persisted_and_only_ever_rises(repo: Repository, server_limits):
    _account, _policy, ctx, _engine, _signal, position, _adapter = await open_position(
        repo, server_limits
    )

    raised = await repo.update_position_peak(position, Decimal("70000"))
    lowered = await repo.update_position_peak(raised, Decimal("60000"))

    assert raised.peak_price == Decimal("70000")
    # A pullback must not lower the high-water mark, or the trail would walk
    # backwards and exit on a move the position had already survived.
    assert lowered.peak_price == Decimal("70000")


async def test_a_pre_v2_position_keeps_its_original_stop(repo: Repository, server_limits):
    """Rows written before migration 0009 have NULL ratchets: that is v1."""

    _account, policy, ctx, _engine, _signal, position, _adapter = await open_position(
        repo, server_limits
    )
    orchestrator = make_orchestrator(repo, ActiveContainer(), position.account_id)

    assert position.breakeven_trigger_bps is None
    assert position.trailing_trigger_bps is None
    entry = position.avg_entry_price
    stop = entry * (Decimal("1") - position.stop_loss_bps / Decimal("10000"))

    just_above = orchestrator._exit_reason(
        position, market_at(ctx, bid=str(stop + Decimal("1"))), None or ctx.market.as_of
    )
    at_stop = orchestrator._exit_reason(position, market_at(ctx, bid=str(stop)), ctx.market.as_of)

    assert just_above is None
    assert at_stop == "stop_loss"
    # A large run-up changes nothing without a ratchet configured.
    assert (
        orchestrator._exit_reason(position, market_at(ctx, bid=str(entry * 2)), ctx.market.as_of)
        == "take_profit"
    )


async def test_the_trail_moves_the_live_stop_and_the_move_is_recorded(
    repo: Repository, server_limits, monkeypatch
):
    _account, policy, ctx, _engine, _signal, position, adapter = await open_position(
        repo, server_limits
    )
    # Give the position a v2 geometry: arm the trail at 50 bps, 40 bps behind.
    async with repo.db.session() as session:
        row = await session.get(type(position), position.id)
        row.trailing_trigger_bps = Decimal("50")
        row.trailing_distance_bps = Decimal("40")
        await session.flush()
    position = await repo.settled_position(position.id)

    events: list[tuple] = []

    async def record(event_type, entity_type, entity_id, payload=None):
        events.append((event_type, entity_type, entity_id, payload))

    container = ActiveContainer()
    container.execution_for_mode = {Mode.PAPER: adapter}
    orchestrator = make_orchestrator(repo, container, position.account_id)
    container.audit = type("Audit", (), {"record": staticmethod(record)})()
    orchestrator.audit = container.audit
    # The exit manager needs the market filters and a snapshot map.
    entry = position.avg_entry_price
    orchestrator.market = type(
        "Market",
        (),
        {"filters": staticmethod(lambda symbol: _filters(symbol))},
    )()

    snapshot = market_at(ctx, bid=str(entry * Decimal("1.02")))
    # The peak is raised by the exit manager itself, from this bid.
    await repo.update_position_peak(position, snapshot.bid)
    position = await repo.settled_position(position.id)
    reason, plan = orchestrator._exit_decision(position, snapshot, ctx.market.as_of)

    assert plan.ratcheted is True
    expected_stop = entry * Decimal("1.02") * (Decimal("1") - Decimal("40") / Decimal("10000"))
    assert plan.stop_price == expected_stop
    # At a bid of +200 bps the trailed stop (+160 bps) is far below: no exit.
    assert reason is None

    _exits, notes = await orchestrator._manage_exits(
        policy=policy, snapshot_map={snapshot.symbol: snapshot}, system=system_snapshot()
    )
    assert any(event[0] == "exit.ratchet" for event in events), (events, notes)

    # A second cycle at the same peak does not duplicate the record.
    await orchestrator._manage_exits(
        policy=policy, snapshot_map={snapshot.symbol: snapshot}, system=system_snapshot()
    )
    ratchets = [event for event in events if event[0] == "exit.ratchet"]
    assert len(ratchets) == 1


async def _filters(symbol: str):
    from conftest import symbol_rules

    return symbol_rules(symbol=symbol)


async def test_the_trail_exits_a_winner_that_round_trips(repo: Repository, server_limits):
    _account, _policy, ctx, _engine, _signal, position, _adapter = await open_position(
        repo, server_limits
    )
    async with repo.db.session() as session:
        row = await session.get(type(position), position.id)
        row.trailing_trigger_bps = Decimal("50")
        row.trailing_distance_bps = Decimal("40")
        await session.flush()
    position = await repo.settled_position(position.id)
    orchestrator = make_orchestrator(repo, ActiveContainer(), position.account_id)

    entry = position.avg_entry_price
    high = entry * Decimal("1.05")
    await repo.update_position_peak(position, high)
    position = await repo.settled_position(position.id)

    # Back to entry: the trail sits at 1.05 - 40 bps = 1.046, well above.
    reason = orchestrator._exit_reason(position, market_at(ctx, bid=str(entry)), ctx.market.as_of)

    assert reason == "stop_loss"


async def test_an_operator_close_still_wins_over_a_quiet_position(
    repo: Repository, server_limits
):
    """The ratchet must not make a manual close unreachable."""

    _account, _policy, ctx, _engine, _signal, position, _adapter = await open_position(
        repo, server_limits
    )
    await repo.request_close(position.id, reason="operator", actor="test:operator")
    position = await repo.settled_position(position.id)
    orchestrator = make_orchestrator(repo, ActiveContainer(), position.account_id)

    reason, plan = orchestrator._exit_decision(position, ctx.market, ctx.market.as_of)

    assert reason is None
    assert plan.ratcheted is False
    assert position.exit_requested_reason == "operator"
