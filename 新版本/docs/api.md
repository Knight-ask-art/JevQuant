# API contract

Base URL: `http://127.0.0.1:8000`. OpenAPI is served at `/docs`.

## Authentication

| Identity | Header | Used by |
|---|---|---|
| Operator | `X-API-Key: <JEV_ADMIN_API_KEY>` | UI/CLI read and control endpoints |
| Execution gateway | `X-Internal-Key: <JEV_INTERNAL_EXECUTION_KEY>` | `POST /api/v1/execution/intents`, `/execution/reconcile` |

When `JEV_AUTH_DISABLED=true` (local default), operator endpoints are open and the
execution key is still required for the internal dispatch endpoint. An optional
`X-Actor` header records the operator label in audit events (max 64 chars).

WebSocket authentication uses `?api_key=...` because browsers cannot attach headers to a
WebSocket handshake. Close code `4401` means unauthorized.

Secrets are never returned by any endpoint.

## Error shape

```json
{
  "error": {
    "code": "RISK_REJECTED",
    "message": "Human-readable message.",
    "retryable": false,
    "retry_after_ms": null,
    "trace_id": "uuid",
    "details": {}
  }
}
```

`X-Trace-Id` is always present. Validation failures use HTTP 422 with
`code: "VALIDATION_ERROR"`. Unhandled errors return HTTP 500 with
`code: "INTERNAL_ERROR"` and no internal details.

## System

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | liveness; process and container are up |
| GET | `/readyz` | readiness; database and market feed reachable |
| GET | `/api/v1/system/status` | mode, dependencies, account, positions, policy, limits |
| GET | `/api/v1/system/config` | redacted configuration; never contains secrets |
| POST | `/api/v1/demo/cycle?refresh_policy=false` | run one cycle; refused in live mode |

`/api/v1/system/status` is the UI's primary poll (8 s).

## Modes, kill switch and live arming

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/v1/mode/set` | `{"mode":"paper"\|"shadow"\|"testnet"\|"live"}` | `live` returns `MODE_NOT_ARMED`; 409 while a live position is open |
| POST | `/api/v1/mode/preflight-live` | — | returns `{passed, checks[], arming_token}`; token is null on failure |
| POST | `/api/v1/mode/arm-live` | `{"arming_token","confirmation","totp_code"}` | confirmation must be `ARM LIVE TRADING` |
| POST | `/api/v1/mode/disarm` | `{"reason"}` | stops new entries; keeps mode for exits |
| POST | `/api/v1/kill-switch` | `{"reason"}` | stops new entries; keeps mode for exits |
| POST | `/api/v1/kill-switch/reset` | — | clears the switch |

The arming token is single-use, short-lived and held only in memory by the client until
redeemed. A failed redemption requires a fresh preflight.

## Strategy policy

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/strategy/policies/generate` | `{"purpose":"trade_runtime"\|"strategy_authoring"}`; compile + persist |
| GET | `/api/v1/strategy/policies/current` | active row plus compiled description |
| GET | `/api/v1/strategy/policies?limit=50` | history |
| GET | `/api/v1/strategy/policies/{policy_id}` | one policy |

`generate` returns the **effective** policy, the raw model proposal, and every clamp. A
purpose denied by the provider is rejected with `PURPOSE_NOT_ALLOWED` (HTTP 403).

## Signals and decisions

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/signals?limit=100&state=ELIGIBLE` | signal tape |
| GET | `/api/v1/signals/{signal_id}` | signal, events, Jev evaluation, intent, risk decision, order |
| GET | `/api/v1/intents?limit=100` | intents |
| GET | `/api/v1/risk-decisions?limit=100` | risk decisions |

## Execution and ledger

| Method | Path | Identity | Purpose |
|---|---|---|---|
| POST | `/api/v1/execution/intents` | internal | dispatch an approved intent: `{"intent_id":"..."}` |
| POST | `/api/v1/execution/reconcile` | internal | ask the adapter to re-check unknown orders |
| GET | `/api/v1/execution/health` | operator | adapter health for the active mode |
| POST | `/api/v1/execution/balances/sync` | operator | read testnet/live venue balances, record + audit drift |
| GET | `/api/v1/execution/balances` | operator | latest venue balance snapshot per route/asset |
| GET | `/api/v1/orders?limit=100` | operator | orders |
| GET | `/api/v1/orders/{order_id}` | operator | one order |
| GET | `/api/v1/fills?limit=100` | operator | fills |
| GET | `/api/v1/positions?status=OPEN` | operator | positions with marks/unrealized |
| GET | `/api/v1/pnl?limit=200` | operator | PnL snapshots and account totals |

Dispatch rules: the intent must exist, be `APPROVED`, have a persisted approved risk
decision, be unexpired, and match the active mode. Unknown outcomes return
`RECONCILIATION_REQUIRED`; they never fabricate a fill. The response `status` is the
signal state after dispatch and `intent_status` is the intent's final status.

`POST /api/v1/demo/cycle` returns HTTP 409 while another cycle is running.

## Backtests

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/backtests` | run a deterministic synthetic replay to completion |
| GET | `/api/v1/backtests?limit=50` | history |
| GET | `/api/v1/backtests/{run_id}` | one run |

Body:

```json
{
  "symbols": ["BTCUSDT"],
  "minutes": 5,
  "candles": 1500,
  "starting_equity_usdt": "10000",
  "max_position_notional_usdt": "500",
  "stop_loss_bps": "120",
  "take_profit_bps": "240",
  "max_holding_minutes": 240,
  "fee_bps": "10",
  "slippage_bps": "2",
  "seed": 42
}
```

Every report includes `"is_synthetic": true`, the exact spec and an `assumptions` list.
Symbols outside `JEV_ALLOWED_SYMBOLS` are rejected.

## Audit

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/audit?limit=100&event_type=risk.decision` | hash-linked events |
| GET | `/api/v1/audit/verify` | re-walk the chain |

Verification returns `{"valid": true, "count": n, "head": "sha256:..."}` or
`{"valid": false, "count": n, "broken_seq": k}`.

## Connections

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/connections/opencode/test` | adapter health; no secrets |
| GET | `/api/v1/connections/opencode/models` | model catalogue, or empty for simulated |
| POST | `/api/v1/connections/binance/test` | key permission inspection; HTTP 451 is reported |
| POST | `/api/v1/connections/jev/test` | Jev adapter health |
| GET | `/api/v1/connections/hummingbot/health` | gateway health |

## WebSocket

`GET /ws/events` (upgrade) streams broker events:

```json
{"topic": "system.events", "ts": "...", "payload": {"event": "cycle.completed", "...": "..."}}
```

Topics: `market.features`, `policy.updated`, `signal.candidates`,
`trade.intents.approved`, `execution.events`, `audit.ingest`, `system.events`.
A `heartbeat` event is sent after 15 s without traffic. The stream is read-only.

## Money representation

Every price, quantity, notional, fee and PnL value is returned as a canonical decimal
**string**, never a JSON float. Clients must not parse these into binary floats for
sizing; display rounding is a UI concern.
