"""Execution telemetry: which stage took the time, and what it refuses to guess.

The report is evidence, and evidence is only worth reading if it says what it
does *not* know. Two rules carry that here: a stage with no boundary in the
trail contributes no sample, and a venue call nobody measured is not a zero.
Both mistakes would make the pipeline look faster than it is.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from jev_trading.adapters.execution.base import ExecutionReceipt
from jev_trading.api.app import create_app
from jev_trading.config import Settings
from jev_trading.domain.enums import OrderStatus, SignalState
from jev_trading.services.telemetry import (
    MIN_SAMPLES_FOR_EVIDENCE,
    ExecutionTelemetry,
    format_report,
)
from jev_trading.storage.db import Database
from jev_trading.storage.repository import Repository

NOW = datetime(2026, 10, 22, 8, 0, tzinfo=UTC)


@pytest.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'telemetry.db'}")
    await db.create_all()
    try:
        yield Repository(db)
    finally:
        await db.dispose()


def signal(*trail: tuple[SignalState, float]) -> SimpleNamespace:
    """A signal whose state trail is (state, seconds-after-NOW) pairs."""

    return SimpleNamespace(
        id=str(uuid4()),
        events=[
            SimpleNamespace(
                state=state.value,
                created_at=NOW + timedelta(seconds=offset),
            )
            for state, offset in trail
        ],
    )


def fully_timed(*, offset: float = 0.0) -> SimpleNamespace:
    """One signal that walked the whole pipeline in five equal seconds."""

    return signal(
        (SignalState.CANDIDATE, offset),
        (SignalState.JEV_EVALUATING, offset),
        (SignalState.SCORED, offset + 5),
        (SignalState.ELIGIBLE, offset + 10),
        (SignalState.RISK_PENDING, offset + 10),
        (SignalState.APPROVED, offset + 15),
        (SignalState.EXECUTION_PENDING, offset + 20),
        (SignalState.ACCEPTED, offset + 25),
    )


def timing(report, stage: str):
    return next(item for item in report["stages"] if item["stage"] == stage)


# -- stages are subtractions -------------------------------------------------
def test_a_stage_is_the_time_between_its_two_states():
    telemetry = ExecutionTelemetry(signals=[fully_timed()], venue_latencies_ms=[])

    report = telemetry.report()

    assert timing(report, "jev_evaluation")["median_seconds"] == 5.0
    assert timing(report, "gating")["median_seconds"] == 5.0
    assert timing(report, "risk_approval")["median_seconds"] == 5.0
    assert timing(report, "dispatch")["median_seconds"] == 5.0
    assert timing(report, "execution")["median_seconds"] == 5.0


def test_a_signal_that_fills_directly_ends_execution_at_filled():
    """A market fill can skip ACCEPTED; the trail still has a boundary."""

    telemetry = ExecutionTelemetry(
        signals=[
            signal(
                (SignalState.EXECUTION_PENDING, 0),
                (SignalState.FILLED, 3),
                (SignalState.POSITION_OPEN, 3),
            )
        ],
        venue_latencies_ms=[],
    )

    execution = timing(telemetry.report(), "execution")

    assert execution["samples"] == 1
    assert execution["median_seconds"] == 3.0


def test_a_missing_boundary_is_no_sample_rather_than_zero():
    """A signal blocked before dispatch must not look like instant dispatch."""

    telemetry = ExecutionTelemetry(
        signals=[signal((SignalState.CANDIDATE, 0), (SignalState.SCORED, 4))],
        venue_latencies_ms=[],
    )

    report = telemetry.report()

    assert timing(report, "jev_evaluation")["samples"] == 1
    dispatch = timing(report, "dispatch")
    assert dispatch["samples"] == 0
    assert dispatch["median_seconds"] is None
    assert dispatch["max_seconds"] is None


def test_a_trail_that_runs_backwards_contributes_nothing():
    """Clock skew inside a cycle is not a negative duration to average in."""

    telemetry = ExecutionTelemetry(
        signals=[
            signal(
                (SignalState.CANDIDATE, 10),
                (SignalState.SCORED, 4),
            )
        ],
        venue_latencies_ms=[],
    )

    assert timing(telemetry.report(), "jev_evaluation")["samples"] == 0


def test_only_the_first_time_a_state_was_reached_is_used():
    """A signal may pass through a state again; the stage is its first pass."""

    telemetry = ExecutionTelemetry(
        signals=[
            signal(
                (SignalState.EXECUTION_PENDING, 0),
                (SignalState.ACCEPTED, 2),
                (SignalState.FILLED, 9),
            )
        ],
        venue_latencies_ms=[],
    )

    assert timing(telemetry.report(), "execution")["median_seconds"] == 2.0


# -- the venue call ----------------------------------------------------------
def test_an_unmeasured_venue_call_is_missing_not_zero():
    report = ExecutionTelemetry(signals=[], venue_latencies_ms=[]).report()

    assert report["venue"] == {"calls": 0, "median_ms": None, "p90_ms": None, "max_ms": None}


def test_venue_latency_reports_the_calls_the_adapter_measured():
    telemetry = ExecutionTelemetry(
        signals=[], venue_latencies_ms=[10.0, 20.0, 30.0, 400.0]
    )

    venue = telemetry.venue_report()

    assert venue["calls"] == 4
    assert venue["median_ms"] == 25.0
    # Nearest rank: the 4th value is the 90th percentile of four samples.
    assert venue["p90_ms"] == 400.0
    assert venue["max_ms"] == 400.0


# -- what the report says about itself ---------------------------------------
def test_slowest_stage_is_ranked_by_median_not_by_max():
    """One hung call is an incident; the report describes the pipeline."""

    def two_stage_signal(jev: float, risk: float, offset: float) -> SimpleNamespace:
        return signal(
            (SignalState.CANDIDATE, offset),
            (SignalState.JEV_EVALUATING, offset),
            (SignalState.SCORED, offset + jev),
            (SignalState.ELIGIBLE, offset + jev),
            (SignalState.RISK_PENDING, offset + jev),
            (SignalState.APPROVED, offset + jev + risk),
        )

    steady = [two_stage_signal(8, 5, offset) for offset in range(5)]
    outlier = two_stage_signal(1, 60, 100)  # risk waited a minute, once

    report = ExecutionTelemetry(
        signals=[*steady, outlier], venue_latencies_ms=[]
    ).report()

    # The largest single sample belongs to risk_approval; its median does not.
    assert timing(report, "risk_approval")["max_seconds"] == 60.0
    assert timing(report, "risk_approval")["median_seconds"] == 5.0
    assert report["slowest_stage"] == "jev_evaluation"


def test_adequacy_needs_the_minimum_sample_count():
    few = [fully_timed(offset=index) for index in range(MIN_SAMPLES_FOR_EVIDENCE - 1)]
    enough = [fully_timed(offset=index) for index in range(MIN_SAMPLES_FOR_EVIDENCE)]

    assert ExecutionTelemetry(signals=few, venue_latencies_ms=[]).adequacy() == "insufficient"
    assert ExecutionTelemetry(signals=enough, venue_latencies_ms=[]).adequacy() == "adequate"


def test_an_empty_window_says_it_has_nothing_rather_than_being_fast():
    report = ExecutionTelemetry(signals=[], venue_latencies_ms=[]).report()

    assert report["signals_read"] == 0
    assert report["sample_adequacy"] == "insufficient"
    assert report["slowest_stage"] is None
    assert all(stage["samples"] == 0 for stage in report["stages"])


def test_the_text_report_states_what_it_could_not_measure():
    report = ExecutionTelemetry(signals=[fully_timed()], venue_latencies_ms=[]).report()

    text = format_report(report)

    assert "Venue calls measured: 0" in text
    assert "insufficient" in text
    assert "not of the pipeline" in text


# -- the persisted measurement ----------------------------------------------
async def test_the_latency_query_reads_only_measured_calls(repo):
    for value in (12, 34):
        order_id = str(uuid4())
        await repo.record_order_event(order_id, "EXECUTION_LATENCY", {"latency_ms": value})
    # A simulated adapter records its events but never a latency.
    await repo.record_order_event(str(uuid4()), "STATUS_CHANGE", {"status": "ACCEPTED"})
    # A malformed payload is not a measurement either.
    await repo.record_order_event(str(uuid4()), "EXECUTION_LATENCY", {})

    assert sorted(await repo.list_execution_latencies()) == [12.0, 34.0]


async def test_the_signal_query_attaches_the_trail_it_was_asked_for(repo):
    policy_id, snapshot_id = str(uuid4()), str(uuid4())
    older = await repo.create_signal(
        symbol="BTCUSDT",
        policy_id=policy_id,
        snapshot_id=snapshot_id,
        side="BUY",
        created_at=NOW - timedelta(hours=2),
    )
    await repo.transition_signal(older.id, SignalState.JEV_EVALUATING)
    await repo.transition_signal(older.id, SignalState.SCORED)
    outside = await repo.create_signal(
        symbol="BTCUSDT",
        policy_id=policy_id,
        snapshot_id=snapshot_id,
        side="BUY",
        created_at=NOW - timedelta(days=30),
    )

    rows = await repo.list_signals_with_events(
        since=NOW - timedelta(days=1), until=NOW
    )

    assert [row.id for row in rows] == [older.id]
    assert [event.state for event in rows[0].events] == ["CANDIDATE", "JEV_EVALUATING", "SCORED"]
    assert outside.id not in [row.id for row in rows]


async def test_an_adapter_that_did_not_measure_leaves_no_row(repo):
    from test_exit_lifecycle import ActiveContainer, make_orchestrator

    orchestrator = make_orchestrator(repo, ActiveContainer(), "acct")

    measured = ExecutionReceipt(
        order_id=str(uuid4()), status=OrderStatus.ACCEPTED, latency_ms=42
    )
    unmeasured = ExecutionReceipt(order_id=str(uuid4()), status=OrderStatus.ACCEPTED)

    await orchestrator._record_latency(measured)
    await orchestrator._record_latency(unmeasured)

    assert await repo.list_execution_latencies() == [42.0]


# -- the route ---------------------------------------------------------------
def test_the_telemetry_route_reports_an_empty_window_honestly(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'telemetry-api.db'}",
        synthetic_market=True,
        demo_mode=True,
        auth_disabled=True,
        admin_api_key="test-admin-key",
        credential_encryption_key="J7GDQdV_ZVK-9RoVTmiEZpiDSWEEtXzEI_YcGsUuDmY=",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/execution-telemetry")

        assert response.status_code == 200
        body = response.json()
        assert body["signals_read"] == 0
        assert body["venue"]["calls"] == 0
        assert body["slowest_stage"] is None

        assert client.get("/api/v1/execution-telemetry?days=0").status_code == 422
