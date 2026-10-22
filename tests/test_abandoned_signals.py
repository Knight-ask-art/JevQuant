"""Signals a dead process left mid-pipeline are closed, and only those.

The failure this guards against is silent: a row in ``JEV_EVALUATING`` or
``EXECUTION_PENDING`` looks like work in progress forever, keeps counting as
"reached the market" for pacing, and is never noticed by any report. The cost
of over-correcting is worse — closing a signal that is genuinely in flight
would race the real writer — so the boundaries matter as much as the sweep.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import NOW

from jev_trading.domain.enums import SignalState
from jev_trading.services.abandoned_signals import (
    DEFAULT_ABANDON_AFTER,
    AbandonedSignal,
    abandoned_of,
    abandoned_state,
    is_abandoned,
)
from jev_trading.storage.db import Database
from jev_trading.storage.repository import Repository

from test_exit_lifecycle import ActiveContainer, make_orchestrator


@pytest.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'abandoned.db'}")
    await db.create_all()
    try:
        yield Repository(db)
    finally:
        await db.dispose()


def event(state: str, at: datetime) -> SimpleNamespace:
    return SimpleNamespace(state=state, created_at=at)


# -- the decision ------------------------------------------------------------
def test_an_in_flight_row_past_the_threshold_is_abandoned():
    assert is_abandoned(
        state="JEV_EVALUATING",
        last_seen=NOW - DEFAULT_ABANDON_AFTER,
        now=NOW,
    )


def test_a_row_that_moved_recently_is_left_alone():
    """Ten minutes is far above every real step; a row inside it is live work."""

    assert not is_abandoned(
        state="EXECUTION_PENDING",
        last_seen=NOW - timedelta(seconds=30),
        now=NOW,
    )


@pytest.mark.parametrize(
    "state",
    ["CANDIDATE", "SCORED", "REJECTED", "APPROVED", "ACCEPTED", "FILLED",
     "POSITION_OPEN", "CLOSED", "ERROR", "RECONCILIATION_REQUIRED"],
)
def test_a_terminal_or_pre_gate_state_is_never_abandoned(state):
    """Only states whose next write is part of the same flow are swept."""

    assert state not in {
        "JEV_EVALUATING", "INTENT_CREATED", "RISK_PENDING", "EXECUTION_PENDING"
    }
    assert not is_abandoned(state=state, last_seen=NOW - timedelta(days=7), now=NOW)


def test_a_clock_skewed_timestamp_is_not_evidence_of_abandonment():
    assert not is_abandoned(
        state="JEV_EVALUATING", last_seen=NOW + timedelta(hours=1), now=NOW
    )


def test_dispatch_is_sent_to_reconciliation_not_to_error():
    """The venue may hold the order; only a reconcile pass can settle that."""

    assert abandoned_state("EXECUTION_PENDING") == "RECONCILIATION_REQUIRED"
    assert abandoned_state("JEV_EVALUATING") == "ERROR"
    assert abandoned_state("RISK_PENDING") == "ERROR"


def test_the_newest_event_decides_the_idle_time():
    """A row created long ago but advanced recently is still working."""

    rows = [
        (
            "signal-1",
            "BTCUSDT",
            "EXECUTION_PENDING",
            [event("CANDIDATE", NOW - timedelta(hours=3)),
             event("EXECUTION_PENDING", NOW - timedelta(seconds=20))],
        )
    ]

    assert abandoned_of(rows, now=NOW) == []


def test_a_trail_with_no_timestamp_is_left_alone():
    rows = [("signal-1", "BTCUSDT", "JEV_EVALUATING", [])]

    assert abandoned_of(rows, now=NOW) == []


def test_the_selection_reports_where_it_moved_each_row():
    rows = [
        ("signal-1", "BTCUSDT", "JEV_EVALUATING", [event("JEV_EVALUATING", NOW - timedelta(hours=1))]),
        ("signal-2", "ETHUSDT", "EXECUTION_PENDING", [event("EXECUTION_PENDING", NOW - timedelta(days=2))]),
    ]

    found = abandoned_of(rows, now=NOW)

    assert {item.signal_id: item.to_state for item in found} == {
        "signal-1": "ERROR",
        "signal-2": "RECONCILIATION_REQUIRED",
    }
    assert found[0].as_detail()["reason"] == "abandoned_in_flight"


# -- the repository query ----------------------------------------------------
async def test_the_query_returns_only_in_flight_rows_with_their_trail(repo):
    stuck = await repo.create_signal(
        symbol="BTCUSDT", policy_id="p", snapshot_id="s", side="buy"
    )
    await repo.transition_signal(stuck.id, SignalState.JEV_EVALUATING)
    done = await repo.create_signal(
        symbol="ETHUSDT", policy_id="p", snapshot_id="s", side="buy"
    )

    rows = await repo.list_in_flight_signals(("JEV_EVALUATING", "EXECUTION_PENDING"))

    assert [row.id for row in rows] == [stuck.id]
    assert [item.state for item in rows[0].events] == ["CANDIDATE", "JEV_EVALUATING"]
    assert done.id not in {row.id for row in rows}
    assert await repo.list_in_flight_signals(()) == []


# -- the sweep, through the orchestrator --------------------------------------
async def _stuck_signal(repo, *, state: SignalState, minutes_ago: int) -> str:
    signal = await repo.create_signal(
        symbol="BTCUSDT", policy_id="p", snapshot_id="s", side="buy"
    )
    for step in (SignalState.JEV_EVALUATING, SignalState.SCORED, SignalState.ELIGIBLE,
                 SignalState.INTENT_CREATED, SignalState.RISK_PENDING,
                 SignalState.APPROVED, SignalState.EXECUTION_PENDING):
        await repo.transition_signal(signal.id, step)
        if step is state:
            break
    # Backdate the whole trail: the sweep reads event timestamps, not rows.
    async with repo.db.session() as session:
        from sqlalchemy import update

        from jev_trading.storage.models import SignalEventRow

        await session.execute(
            update(SignalEventRow)
            .where(SignalEventRow.signal_id == signal.id)
            .values(created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago))
        )
    return signal.id


async def test_the_sweep_closes_a_stuck_row_and_records_it(repo):
    stuck_id = await _stuck_signal(repo, state=SignalState.EXECUTION_PENDING, minutes_ago=45)
    events: list[tuple] = []

    async def record(event_type, entity_type, entity_id, payload=None):
        events.append((event_type, entity_id, payload))

    container = ActiveContainer()
    container.audit = SimpleNamespace(record=record)
    orchestrator = make_orchestrator(repo, container, "acct")

    moved = await orchestrator.abandon_stuck_signals(datetime.now(UTC))

    assert [item.signal_id for item in moved] == [stuck_id]
    after = await repo.get_signal(stuck_id)
    assert after.state == SignalState.RECONCILIATION_REQUIRED.value
    assert [item[0] for item in events] == ["signal.abandoned"]


async def test_the_sweep_leaves_a_row_that_is_still_working(repo):
    await _stuck_signal(repo, state=SignalState.JEV_EVALUATING, minutes_ago=2)
    orchestrator = make_orchestrator(repo, ActiveContainer(), "acct")

    assert await orchestrator.abandon_stuck_signals(datetime.now(UTC)) == []


async def test_the_sweep_does_not_raise_when_the_query_fails(repo):
    orchestrator = make_orchestrator(repo, ActiveContainer(), "acct")

    async def broken(states):
        raise RuntimeError("database is down")

    orchestrator.repo = SimpleNamespace(list_in_flight_signals=broken)

    assert await orchestrator.abandon_stuck_signals(datetime.now(UTC)) == []


def test_the_threshold_is_wider_than_the_jev_call_can_be():
    """It is a backstop for the process, not a replacement for a timeout."""

    assert DEFAULT_ABANDON_AFTER >= timedelta(minutes=5)
    assert AbandonedSignal(
        signal_id="s", symbol="BTCUSDT", state="JEV_EVALUATING", last_seen=NOW
    ).awaiting_venue is False


async def test_a_cycle_sweeps_before_it_reads_the_tape(
    tmp_path, monkeypatch
):
    """The hook is what makes this a repair rather than a manual command."""

    from sqlalchemy import update

    from test_regime_gate_cycle import _FrozenDatetime, _cycle

    from jev_trading.services import orchestrator as orchestrator_module
    from jev_trading.storage.db import Database
    from jev_trading.storage.models import SignalEventRow

    # Seed the database the helper will open, with one stuck row.
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'regime.db'}")
    await db.create_all()
    seeded = Repository(db)
    stuck = await seeded.create_signal(
        symbol="BTCUSDT", policy_id="p", snapshot_id="s", side="buy"
    )
    await seeded.transition_signal(stuck.id, SignalState.JEV_EVALUATING)
    async with db.session() as session:
        await session.execute(
            update(SignalEventRow)
            .where(SignalEventRow.signal_id == stuck.id)
            .values(created_at=NOW - timedelta(hours=3))
        )
    await db.dispose()

    monkeypatch.setattr(orchestrator_module, "datetime", _FrozenDatetime)
    result, signals, audit = await _cycle(tmp_path, monkeypatch, atr_bps=100.0, enforce=False)

    assert [event for event in audit.events() if event == "signal.abandoned"]
    assert any("abandoned" in note for note in result.notes), result.notes
    moved = [row for row in signals if row.id == stuck.id]
    assert moved[0].state == SignalState.ERROR.value
