"""Control-plane contract tests against an in-process ASGI app."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jev_trading.adapters.models.base import PolicyGenerationResult
from jev_trading.api.app import create_app
from jev_trading.config import Settings

ADMIN = "test-admin-key"
INTERNAL = "test-internal-key"
FERNET_TEST_KEY = "J7GDQdV_ZVK-9RoVTmiEZpiDSWEEtXzEI_YcGsUuDmY="


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'api.db'}",
        "synthetic_market": True,
        "demo_mode": True,
        "auth_disabled": True,
        "admin_api_key": ADMIN,
        "credential_encryption_key": FERNET_TEST_KEY,
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
    assert body["agent"]["provider"] == "simulated"
    assert "get_candles" in body["agent"]["tools"]


def test_edge_shadow_route_lives_where_the_docs_say_it_does(open_client):
    """The route is under the activity router's own prefix.

    Documented once as ``/api/v1/activity/edge-shadow`` and shipped without
    it; a path that silently 404s is worse in a runbook than no path at all.
    """

    assert open_client.get("/api/v1/activity/edge-shadow").status_code == 404
    listed = open_client.get("/api/v1/edge-shadow")
    assert listed.status_code == 200
    assert listed.json() == {"items": []}


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


def test_an_expired_policy_is_reported_expired_not_active(open_client):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from jev_trading.storage.models import StrategyPolicyRow

    response = open_client.post(
        "/api/v1/strategy/policies/generate", json={"purpose": "trade_runtime"}
    )
    assert response.status_code == 200

    container = open_client.app.state.container

    async def _expire_policy():
        async with container.db.session() as session:
            row = (
                await session.execute(
                    select(StrategyPolicyRow).order_by(
                        StrategyPolicyRow.created_at.desc()
                    )
                )
            ).scalars().first()
            assert row is not None
            expires = datetime.now(UTC) - timedelta(minutes=1)
            row.expires_at = expires
            row.effective_json = {**row.effective_json, "expires_at": expires.isoformat()}

    open_client.portal.call(_expire_policy)

    current = open_client.get("/api/v1/strategy/policies/current").json()
    assert current["active"] is False
    assert current["expired"] is True
    assert current["reason"] == "expired"
    assert current["policy"]["status"] == "EXPIRED"

    status = open_client.get("/api/v1/system/status").json()
    assert status["policy"] is None
    assert status["policy_state"]["active"] is False
    assert status["policy_state"]["expired"] is True
    assert status["policy_state"]["status"] == "EXPIRED"
    assert status["dependencies"]["policy_fresh"] is False

    history = open_client.get("/api/v1/strategy/policies").json()["items"]
    assert history[0]["status"] == "EXPIRED"
    assert history[0]["expired"] is True


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
    # Nothing drifted, so nothing was reconciled.
    assert body["reconciled"] == {}

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
        with pytest.raises(WebSocketDisconnect):
            with api.websocket_connect(f"/ws/events?api_key={ADMIN}") as socket:
                socket.receive_json()
        login = api.post("/api/v1/auth/login", json={"api_key": ADMIN})
        assert login.status_code == 200
        with api.websocket_connect("/ws/events") as socket:
            event = socket.receive_json()
            assert event["payload"]["event"] == "stream.open"


def test_model_settings_are_write_only_and_kept_on_later_saves(open_client):
    response = open_client.put(
        "/api/v1/settings/models",
        json={
            "llm": {
                "base_url": "http://127.0.0.1:9",
                "api_key": "secret-llm-key",
                "model": "test-model",
                "protocol": "openai",
            },
            "jev": {
                "base_url": "http://127.0.0.1:9",
                "api_key": "secret-jev-key",
                "model": "jev-test",
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["models"]["llm"]["configured"] is True
    assert body["models"]["llm"]["api_key_configured"] is True
    assert body["models"]["jev"]["configured"] is True
    # The secret goes in and never comes back out.
    assert "secret-llm-key" not in response.text
    assert "secret-jev-key" not in response.text
    # The underlying system_state must contain ciphertext, not model secrets.
    container = open_client.app.state.container
    persisted = open_client.portal.call(container.repo.get_state, "runtime_credentials")
    assert persisted["llm"]["api_key"].startswith("fernet:v1:")
    assert persisted["jev"]["api_key"].startswith("fernet:v1:")
    assert "secret-llm-key" not in str(persisted)
    assert "secret-jev-key" not in str(persisted)
    assert "api_key" not in body["models"]["llm"]

    # An omitted api_key keeps the stored secret, because the client cannot
    # echo what it never received.
    again = open_client.put(
        "/api/v1/settings/models",
        json={"llm": {"base_url": "http://127.0.0.1:9", "model": "test-model-2"}},
    )
    assert again.status_code == 200
    assert again.json()["models"]["llm"]["api_key_configured"] is True
    assert again.json()["models"]["llm"]["model"] == "test-model-2"
    assert "secret-llm-key" not in again.text

    config = open_client.get("/api/v1/system/config").json()["config"]
    assert config["opencode_console_configured"] is True
    assert config["jev_configured"] is True


def test_model_settings_reject_non_http_urls_and_can_be_cleared(open_client):
    bad = open_client.put(
        "/api/v1/settings/models", json={"llm": {"base_url": "ftp://models.local"}}
    )
    assert bad.status_code == 422

    open_client.put(
        "/api/v1/settings/models",
        json={"jev": {"base_url": "http://127.0.0.1:9", "api_key": "k", "model": "m"}},
    )
    cleared = open_client.put(
        "/api/v1/settings/models", json={"jev": {"clear_api_key": True}}
    )
    assert cleared.status_code == 200
    assert cleared.json()["models"]["jev"]["api_key_configured"] is False
    assert cleared.json()["models"]["jev"]["configured"] is False


def test_model_settings_survive_a_restart(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as api:
        assert api.put(
            "/api/v1/settings/models",
            json={
                "llm": {
                    "base_url": "http://127.0.0.1:9",
                    "api_key": "persisted-key",
                    "model": "persisted-model",
                }
            },
        ).status_code == 200
    with TestClient(create_app(make_settings(tmp_path))) as api:
        restored = api.get("/api/v1/settings/models").json()
        assert restored["llm"]["configured"] is True
        assert restored["llm"]["model"] == "persisted-model"
        assert "persisted-key" not in str(restored)


def test_automation_can_be_switched_without_a_restart(open_client):
    enabled = open_client.put(
        "/api/v1/settings/automation",
        json={"enabled": True, "interval_seconds": 600},
    )
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert enabled.json()["next_run_at"]

    status = open_client.get("/api/v1/system/status").json()
    assert status["automation"]["enabled"] is True
    assert status["automation"]["interval_seconds"] == 600

    disabled = open_client.put(
        "/api/v1/settings/automation",
        json={"enabled": False, "interval_seconds": 600},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["next_run_at"] is None


def test_the_persisted_automation_switch_survives_a_restart(tmp_path):
    with client(tmp_path) as api:
        api.put(
            "/api/v1/settings/automation",
            json={"enabled": True, "interval_seconds": 600},
        )
    with client(tmp_path) as api:
        status = api.get("/api/v1/system/status").json()
        assert status["automation"]["enabled"] is True
        assert status["automation"]["interval_seconds"] == 600
        # Switch it off, then restart: an "off" chosen by an operator stays off.
        api.put(
            "/api/v1/settings/automation",
            json={"enabled": False, "interval_seconds": 600},
        )
    with client(tmp_path) as api:
        assert api.get("/api/v1/system/status").json()["automation"]["enabled"] is False


def test_a_configured_auto_interval_waits_for_the_operator(tmp_path):
    with client(tmp_path, auto_cycle_seconds=60) as api:
        # The env interval is not resurrected by a restart on its own.
        assert api.get("/api/v1/system/status").json()["automation"]["enabled"] is False


def test_symbol_universe_narrows_the_hard_envelope(open_client):
    listing = open_client.get("/api/v1/market/universe").json()
    assert "BTCUSDT" in listing["symbols"]

    changed = open_client.put(
        "/api/v1/market/universe", json={"symbols": ["ETHUSDT", "ethusdt"]}
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["symbols"] == ["ETHUSDT"]

    status = open_client.get("/api/v1/system/status").json()
    assert status["server_limits"]["allowed_symbols"] == ["ETHUSDT"]

    # A policy generated afterwards cannot propose BTC: the compiler clamps
    # it out, because the operator's list is the server envelope.
    policy = open_client.post(
        "/api/v1/strategy/policies/generate", json={"purpose": "trade_runtime"}
    ).json()
    universe = policy["effective"]["universe"]
    assert [item["symbol"] for item in universe] == ["ETHUSDT"]

    cycle = open_client.post("/api/v1/demo/cycle").json()
    assert "ETHUSDT" in cycle["symbols"]
    assert "BTCUSDT" not in cycle["symbols"]

    signals = open_client.get("/api/v1/signals?limit=50").json()["items"]
    assert signals and {item["symbol"] for item in signals} == {"ETHUSDT"}


def test_symbol_universe_rejects_non_usdt_symbols(open_client):
    bad = open_client.put("/api/v1/market/universe", json={"symbols": ["BTCBUSD"]})
    assert bad.status_code == 422
    empty = open_client.put("/api/v1/market/universe", json={"symbols": []})
    assert empty.status_code == 422


def test_candles_are_closed_and_upsampled(open_client):
    response = open_client.get(
        "/api/v1/market/candles?symbol=BTCUSDT&interval=15m&limit=40"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["symbol"] == "BTCUSDT"
    assert body["interval"] == "15m"
    assert body["synthetic"] is True
    candles = body["candles"]
    assert 20 <= len(candles) <= 40
    for candle in candles:
        assert candle["l"] <= min(candle["o"], candle["c"]) <= max(candle["o"], candle["c"]) <= candle["h"]
    opens = [candle["t"] for candle in candles]
    assert opens == sorted(opens)

    bad_interval = open_client.get(
        "/api/v1/market/candles?symbol=BTCUSDT&interval=7m"
    )
    assert bad_interval.status_code == 422
    assert bad_interval.json()["error"]["code"] == "VALIDATION_ERROR"


def test_candles_survive_a_restart_with_the_stored_universe(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as api:
        assert api.put(
            "/api/v1/market/universe", json={"symbols": ["SOLUSDT"]}
        ).status_code == 200
    with TestClient(create_app(make_settings(tmp_path))) as api:
        listing = api.get("/api/v1/market/universe").json()
        assert listing["symbols"] == ["SOLUSDT"]
        candles = api.get("/api/v1/market/candles?symbol=BTCUSDT&interval=5m&limit=30")
        assert candles.status_code == 200


def test_manual_close_is_404_for_an_unknown_position(open_client):
    response = open_client.post("/api/v1/execution/positions/does-not-exist/close")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_manual_close_endpoint_closes_an_open_position(open_client):
    # Run cycles until an entry fills, then close it through the operator route.
    for _ in range(30):
        open_client.post("/api/v1/demo/cycle")
        status = open_client.get("/api/v1/system/status").json()
        if status["positions"]:
            break
    positions = open_client.get("/api/v1/positions").json()["items"]
    if not positions:
        pytest.skip("no entry filled within 30 simulated cycles")

    position = positions[0]
    response = open_client.post(f"/api/v1/execution/positions/{position['id']}/close")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["symbol"] == position["symbol"]
    assert body["closed"] is True
    assert body["exits"] == 1

    remaining = open_client.get("/api/v1/positions?status=OPEN").json()["items"]
    assert position["id"] not in {item["id"] for item in remaining}
    audit = open_client.get("/api/v1/audit/verify").json()
    assert audit["valid"] is True


def test_candles_carry_indicators_aligned_to_the_window(open_client):
    response = open_client.get(
        "/api/v1/market/candles?symbol=BTCUSDT&interval=15m&limit=60"
        "&indicators=sma20,sma60,rsi14"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    candles = body["candles"]
    assert len(candles) == 60
    sma = body["indicators"]["sma20"]
    sma60 = body["indicators"]["sma60"]
    rsi = body["indicators"]["rsi14"]
    # Warm-up bars are fetched *before* the visible window, so every indicator
    # is already defined on the first visible bar.
    assert all(point["i"] < len(candles) for point in sma + sma60 + rsi)
    assert sma[0]["i"] == sma60[0]["i"] == rsi[0]["i"] == 0
    assert all(0 <= point["v"] <= 100 for point in rsi)

    # No indicators requested: none returned, no extra work done.
    plain = open_client.get("/api/v1/market/candles?symbol=BTCUSDT&limit=20").json()
    assert plain["indicators"] == {}

    bad = open_client.get(
        "/api/v1/market/candles?symbol=BTCUSDT&limit=20&indicators=ema9"
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


def test_agent_journal_records_what_the_strategy_layer_decided(open_client):
    assert open_client.post("/api/v1/demo/cycle").status_code == 200

    journal = open_client.get("/api/v1/strategy/agent?limit=10")
    assert journal.status_code == 200, journal.text
    body = journal.json()
    assert body["is_agent"] is False  # the deterministic stand-in is running
    assert body["provider"] == "simulated"
    assert sorted(body["tools"]) == [
        "get_account_state",
        "get_blocked_signals",
        "get_candles",
        "get_market_overview",
        "get_open_positions",
        "get_performance_summary",
        "get_prior_strategy",
        "get_recent_decisions",
        "get_recent_evaluations",
        "get_recent_trades",
    ]
    assert body["runs"], "a cycle must leave a journal entry"
    latest = body["latest"]
    assert latest["status"] == "ok"
    assert latest["policy_id"]
    assert latest["tools"] == []  # traces are opt-in per run
    assert latest["tool_count"] == 0
    assert latest["signals"]["created"] >= 1


    # A tool-less provider still records memory: the run carries the policy
    # rationale so the UI can explain the decision without a tool trace.
    assert latest["rationale"]

    detail = open_client.get(f"/api/v1/strategy/agent/{latest['id']}")
    assert detail.status_code == 200
    # Unpriced providers still record what the run would cost: 0 here.
    assert detail.json()["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "__cost_usd__": "0",
    }
    assert isinstance(detail.json()["tools"], list)

    missing = open_client.get("/api/v1/strategy/agent/does-not-exist")
    assert missing.status_code == 404


def test_policy_generation_returns_its_agent_run(open_client):
    response = open_client.post("/api/v1/strategy/policies/generate")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["policy_id"]
    assert body["agent"] is not None
    assert body["agent"]["policy_id"] == body["policy_id"]
    assert body["agent"]["status"] == "ok"
    assert body["agent"]["mode"] == "paper"
    assert body["agent"]["model"] == "simulated-rule-based"
    # Generation is serialized with cycles: a second call must not conflict.
    again = open_client.post("/api/v1/strategy/policies/generate")
    assert again.status_code == 200


def test_a_rejected_policy_is_still_journaled(open_client):
    """A model answer the compiler refuses must not vanish from the journal."""

    container = open_client.app.state.container

    class UnusableGenerator:
        provider = "openai"
        model = "unusable-model"

        async def generate_policy(self, context, **kwargs):
            return PolicyGenerationResult(
                raw={"regime": {"label": "not-a-real-regime"}},
                model=self.model,
                latency_ms=7,
                usage={"input_tokens": 11, "output_tokens": 3},
                simulated=False,
                source="model",
                turns=2,
                trace={"tool_calls": [{"name": "get_candles", "ok": True}]},
            )

    container.policy_generator = UnusableGenerator()
    response = open_client.post("/api/v1/strategy/policies/generate")
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "POLICY_INVALID"

    journal = open_client.get("/api/v1/strategy/agent?limit=5").json()
    run = journal["runs"][0]
    assert run["status"] == "error"
    assert run["error_code"] == "POLICY_INVALID"
    assert run["policy_id"] is None
    # The attempt still cost two model turns and one tool call: say so.
    assert run["turns"] == 2
    assert run["tool_count"] == 1
    assert run["usage"] == {
        "input_tokens": 11,
        "output_tokens": 3,
        "__cost_usd__": "0",
    }


def test_health_probes_are_shared_bounded_and_cached(tmp_path):
    """Several readers share one round trip, and a slow service cannot hold the page."""

    import asyncio

    from jev_trading.services import container as container_module

    calls = {"market": 0, "jev": 0}

    async def slow_jev() -> dict[str, object]:
        calls["jev"] += 1
        await asyncio.sleep(10)
        return {"healthy": True, "provider": "jev"}

    async def flaky_market() -> dict[str, object]:
        calls["market"] += 1
        return {"healthy": calls["market"] == 1, "source": "test"}

    with TestClient(create_app(make_settings(tmp_path))) as api:
        container = api.app.state.container
        container._probe_cache.clear()
        container._probes.clear()
        object.__setattr__(container.market, "health", flaky_market)
        object.__setattr__(container.jev, "health", slow_jev)

        async def read() -> list[dict[str, object]]:
            return await asyncio.gather(
                container._probe("market", container.market.health),
                container._probe("market", container.market.health),
                container._probe("jev", container.jev.health),
                container._probe("jev", container.jev.health),
            )

        market_1, market_2, jev_1, jev_2 = api.portal.call(read)
        # Two readers, one round trip each.
        assert calls == {"market": 1, "jev": 1}
        assert market_1["healthy"] is True and market_2 == market_1
        # The slow service is reported unhealthy instead of hanging the page.
        assert jev_1["healthy"] is False and jev_1["error"] == "TIMEOUT"
        assert jev_2 == jev_1
        # A cached answer is reused until it ages out.
        cached = api.portal.call(
            lambda: container._probe("market", container.market.health)
        )
        assert cached == market_1
        assert calls["market"] == 1


def test_automation_waits_for_the_next_run_it_advertises(tmp_path):
    """The countdown the UI shows is the clock the cycle runs on."""

    from datetime import UTC, datetime, timedelta

    with client(tmp_path) as api:
        container = api.app.state.container
        automation = api.put(
            "/api/v1/settings/automation",
            json={"enabled": True, "interval_seconds": 600},
        ).json()
        advertised = datetime.fromisoformat(automation["next_run_at"])
        # Within a second of the interval the operator chose, not sooner.
        assert timedelta(minutes=5) < advertised - datetime.now(UTC) <= timedelta(minutes=10)

        remaining, _ = container._cycle_countdown()
        assert remaining is not None and 0 < remaining <= 600

        # A stopped clock advertises "expired" and the loop must not skip.
        container._next_cycle_at = datetime.now(UTC) - timedelta(seconds=5)
        remaining, target = container._cycle_countdown()
        assert remaining is None and target is not None
