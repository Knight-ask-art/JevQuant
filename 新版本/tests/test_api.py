"""Control-plane contract tests against an in-process ASGI app."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jev_trading.api.app import create_app
from jev_trading.config import Settings

ADMIN = "test-admin-key"
INTERNAL = "test-internal-key"


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'api.db'}",
        "synthetic_market": True,
        "demo_mode": True,
        "auth_disabled": True,
    }
    values.update(overrides)
    return Settings(**values)


@contextmanager
def client(tmp_path: Path, **overrides):
    with TestClient(create_app(make_settings(tmp_path, **overrides))) as test_client:
        yield test_client


@pytest.fixture
def open_client(tmp_path):
    with client(tmp_path) as test_client:
        yield test_client


def test_health_and_status(open_client):
    assert open_client.get("/healthz").status_code == 200
    ready = open_client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    status = open_client.get("/api/v1/system/status")
    assert status.status_code == 200
    body = status.json()
    assert body["mode"]["mode"] in {"paper", "shadow", "testnet", "live"}
    assert body["synthetic_market"] is True
    assert body["data_source"] == "synthetic"


def test_demo_cycle_produces_signals_and_audit(open_client):
    response = open_client.post("/api/v1/demo/cycle")
    assert response.status_code == 200
    cycle = response.json()
    assert cycle["signals_created"] >= 1
    assert cycle["simulated"] is True
    assert cycle["equity_usdt"]

    signals = open_client.get("/api/v1/signals?limit=10").json()["items"]
    assert signals
    detail = open_client.get(f"/api/v1/signals/{signals[0]['id']}")
    assert detail.status_code == 200
    assert detail.json()["events"]

    audit = open_client.get("/api/v1/audit/verify").json()
    assert audit["valid"] is True
    assert audit["count"] > 0


def test_policy_generation_is_compiled_and_marked(open_client):
    response = open_client.post(
        "/api/v1/strategy/policies/generate", json={"purpose": "trade_runtime"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["policy_id"]
    assert body["simulated"] is True
    assert body["effective"]["clamps"] is not None
    current = open_client.get("/api/v1/strategy/policies/current").json()
    assert current["active"] is True


def test_reconcile_endpoint_uses_the_active_adapter(open_client):
    response = open_client.post(
        "/api/v1/execution/reconcile",
        headers={"X-Internal-Key": "change-me-internal"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["healthy"] is True
    assert "notes" in body


def test_mode_change_is_refused_while_a_live_position_is_open(open_client):
    from datetime import UTC, datetime
    from decimal import Decimal

    container = open_client.app.state.container

    async def _open_live_position():
        async with container.db.session() as session:
            from jev_trading.storage.models import PositionRow

            session.add(
                PositionRow(
                    account_id=container.account_id,
                    symbol="BTCUSDT",
                    qty=Decimal("0.001"),
                    avg_entry_price=Decimal("68000"),
                    stop_loss_bps=Decimal("100"),
                    take_profit_bps=Decimal("200"),
                    max_holding_seconds=3600,
                    status="OPEN",
                    opened_at=datetime.now(UTC),
                    policy_id="p",
                    signal_id="s",
                    mode="live",
                    is_simulated=False,
                )
            )

    open_client.portal.call(_open_live_position)
    response = open_client.post("/api/v1/mode/set", json={"mode": "shadow"})
    assert response.status_code == 409
    assert "live positions" in response.json()["error"]["message"]


def test_balance_sync_endpoint_reports_unconfigured_routes(open_client):
    response = open_client.post("/api/v1/execution/balances/sync")
    assert response.status_code == 200
    body = response.json()
    # Paper/synthetic defaults: neither testnet nor live is configured, so
    # nothing is read and nothing is claimed.
    assert body["drifts"] == []
    assert body["in_tolerance"] is True
    assert set(body["skipped"]) == {"testnet", "live"}
    assert all("not configured" in reason for reason in body["skipped"].values())

    latest = open_client.get("/api/v1/execution/balances")
    assert latest.status_code == 200
    assert latest.json()["items"] == []


def test_backtest_endpoint_returns_synthetic_report(open_client):
    response = open_client.post(
        "/api/v1/backtests",
        json={"symbols": ["BTCUSDT"], "candles": 300, "seed": 3},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["result"]["is_synthetic"] is True
    run = open_client.get(f"/api/v1/backtests/{body['id']}")
    assert run.status_code == 200


def test_backtest_rejects_unknown_symbol(open_client):
    response = open_client.post(
        "/api/v1/backtests", json={"symbols": ["DOGEUSDT"], "candles": 200}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_kill_switch_blocks_entries_and_can_be_reset(open_client):
    killed = open_client.post("/api/v1/kill-switch", json={"reason": "test"})
    assert killed.status_code == 200
    assert killed.json()["kill_switch"] is True

    cycle = open_client.post("/api/v1/demo/cycle").json()
    assert cycle["entries_blocked_reason"] == "kill_switch"

    reset = open_client.post("/api/v1/kill-switch/reset")
    assert reset.status_code == 200
    assert reset.json()["kill_switch"] is False


def test_live_mode_cannot_be_set_directly(open_client):
    response = open_client.post("/api/v1/mode/set", json={"mode": "live"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MODE_NOT_ARMED"


def test_live_preflight_fails_closed_without_credentials(open_client):
    response = open_client.post("/api/v1/mode/preflight-live")
    assert response.status_code == 200
    body = response.json()
    assert body["passed"] is False
    assert body["arming_token"] is None
    assert any(item["name"] == "env_trading_mode_live" and not item["passed"] for item in body["checks"])


def test_auth_requires_admin_key(tmp_path):
    with client(tmp_path, auth_disabled=False, admin_api_key=ADMIN, internal_execution_key=INTERNAL) as api:
        assert api.get("/api/v1/system/status").status_code == 401
        assert (
            api.get("/api/v1/system/status", headers={"X-API-Key": "wrong"}).status_code
            == 401
        )
        ok = api.get("/api/v1/system/status", headers={"X-API-Key": ADMIN})
        assert ok.status_code == 200
        assert api.post("/api/v1/demo/cycle").status_code == 401


def test_internal_execution_identity_is_separate(tmp_path):
    with client(tmp_path, auth_disabled=False, admin_api_key=ADMIN, internal_execution_key=INTERNAL) as api:
        missing = api.post("/api/v1/execution/intents", json={"intent_id": "abc"})
        assert missing.status_code == 401
        with_admin = api.post(
            "/api/v1/execution/intents",
            json={"intent_id": "abc"},
            headers={"X-API-Key": ADMIN},
        )
        assert with_admin.status_code == 401
        internal = api.post(
            "/api/v1/execution/intents",
            json={"intent_id": "does-not-exist"},
            headers={"X-Internal-Key": INTERNAL},
        )
        assert internal.status_code == 404


def test_websocket_exposes_open_event(tmp_path):
    with client(tmp_path, auth_disabled=False, admin_api_key=ADMIN, internal_execution_key=INTERNAL) as api:
        with api.websocket_connect(f"/ws/events?api_key={ADMIN}") as socket:
            event = socket.receive_json()
            assert event["payload"]["event"] == "stream.open"
