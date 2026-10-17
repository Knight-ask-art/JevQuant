# Jev Trading

**LLM strategy authoring + Jev evaluation + independent risk + multi-venue execution.**

This repository implements the design in [`设计.md`](./设计.md): a bounded LLM-authored
strategy policy, a deterministic server-side compiler, typed Jev questions, a risk engine
that is the only component allowed to approve an order, and a single execution contract
shared by Paper, Shadow, Binance Testnet and Hummingbot V2.

> **Status: local paper reference implementation.** The full paper pipeline, control
> plane, audit chain and notebook UI run today against a deterministic synthetic market
> and simulated model adapters. The Binance public/testnet/live and Hummingbot adapters
> have **never made a real exchange request** from this environment — `api.binance.com`
> answers HTTP 451 here, and per the design that is reported rather than circumvented.
> Do not treat any number in the UI as performance evidence. See
> [Honest limitations](#honest-limitations).

---

## What works today

| Area | State |
|---|---|
| Strict `strategy-policy/1.0` schema + compiler that can only narrow | ✅ implemented, tested |
| `trade-intent/1.0` immutable contract, state machine, idempotency key | ✅ implemented, tested |
| Deterministic risk engine (Decimal only, exit-feasibility pre-check) | ✅ implemented, tested |
| Paper adapter with filters, fees, slippage, deterministic partial fills | ✅ implemented, tested |
| Shadow adapter (records intent, never opens a position) | ✅ implemented |
| Binance Testnet adapter (HMAC signing, `newClientOrderId`, reconciliation) | ⚠️ implemented, unexercised |
| Hummingbot V2 adapter (PositionExecutor mapping) | ⚠️ implemented, unexercised |
| Binance key-permission preflight (`enableReading`, spot trading, withdrawals off, IP restrict) | ⚠️ implemented, unexercised |
| FastAPI control plane + WebSocket event stream | ✅ implemented |
| Hash-linked append-only audit + redaction + verification | ✅ implemented, tested |
| Live arming handshake (env flags → preflight → one-time token → phrase + TOTP) | ✅ implemented, cannot pass without real credentials |
| Optional auto-cycle scheduler | ✅ implemented |
| Hand-drawn editorial React UI (8 pages) | ✅ implemented |
| Deterministic synthetic backtest replay | ✅ implemented, explicitly labelled synthetic |
| Real OHLCV / Freqtrade replay | ❌ not implemented |
| Real fill-ledger rebuild for Hummingbot | ❌ follow-up (unknown states are reconciled, not invented) |
| Testnet position/PnL ledger | ❌ order state only; no position projection yet |

## Quick start (local, no Docker, no keys)

Requirements: Python 3.11–3.13, Node 20+.

```powershell
# Backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
jev doctor                 # offline prerequisite check, prints redacted config
jev demo --cycles 5        # paper cycles on the synthetic market
jev serve                  # FastAPI on http://127.0.0.1:8000
```

```powershell
# Frontend (second terminal)
cd frontend
npm install
npm run dev                # http://127.0.0.1:5173 (proxies /api and /ws)
```

Default posture is `paper`, synthetic market, simulated policy author + simulated Jev,
auth disabled for localhost. The UI labels every synthetic/simulated surface.

```powershell
pytest                     # 76 tests: policy, risk, execution, audit, API, TOTP, backtest, exits
```

Or run the whole local check in one step:

```powershell
pwsh -File scripts/check.ps1   # pytest + npm run build
pwsh -File scripts/dev.ps1     # venv, install, doctor, then API + UI
```

### Useful commands

```powershell
jev doctor                     # offline checks + redacted config
jev demo --cycles 40           # advance the synthetic clock and show the ledger
jev cycle                      # one cycle in the configured mode
jev status                     # full status JSON
jev backtest --symbols BTCUSDT --candles 1500
jev serve --host 127.0.0.1 --port 8000
```

## Architecture

```text
market snapshots ──► policy (LLM or simulated author)
       │                    │
       │                    ▼
       │            StrategyPolicy (strict schema)
       │                    │
       │            deterministic compiler  (may only narrow)
       │                    │
       ▼                    ▼
   Jev typed questions ──► gate ──► TradeIntent ──► risk engine
                                                      │ approved
                                                      ▼
                                             execution adapter
                                    paper │ shadow │ testnet │ hummingbot
                                                      │
                                                      ▼
                                        orders/fills/positions/audit
```

Key boundaries:

- **AI cannot execute.** Jev returns a choice distribution, a quality score and a
  yes/no policy fit. It cannot size an order, set a limit, change a policy or touch keys.
- **The compiler is not an advisor.** It clamps expiry, symbols, sides, notional, spread,
  slippage, cooldown, portfolio caps, gate floors and exits; the effective limit is
  `min(model, account, environment, symbol)`.
- **Risk is independent.** It never calls a model, recomputes the risk snapshot hash, and
  is the only producer of `ApprovedTradeIntent`.
- **Failures are asymmetric.** Stale feed/policy/Jev/DB/Redis block *new entries*;
  existing positions keep deterministic exits while execution is healthy.
- **Exits are deterministic.** Stops, take-profits and max holding do not wait for a model.
- **Every decision is auditable.** Audit rows are hash-linked; secrets are redacted before
  persistence; `/api/v1/audit/verify` re-walks the chain.

### Live is a handshake, never a toggle

```text
TRADING_MODE=live + ENABLE_LIVE_TRADING=true
  → server preflight (env, backend=hummingbot, MFA, keys, kill switch, Redis, DB,
    feed, Hummingbot, Binance permissions)
  → single-use arming token (short TTL)
  → operator types "ARM LIVE TRADING"
  → application TOTP
  → atomic server-side arm with an expiry
```

A withdrawal-enabled key, missing IP restriction, unhealthy dependency or active kill
switch refuses arming. Disarming or tripping the kill switch blocks new entries but keeps
the mode so existing positions still have an execution path.

## Repository layout

```text
src/jev_trading/
  domain/       strict contracts: enums, errors, money, policy, market, intent, risk
  adapters/     market (synthetic/binance), models (console/go/jev/simulated), execution
  services/     compiler orchestration, policy, market, modes, audit, events, backtest
  storage/      SQLAlchemy models (18 tables), repository, DB engine
  api/          FastAPI app, auth, routes, serializers, WebSocket
  cli/          `jev` command
frontend/
  src/pages/    Overview, Strategy Desk, Jev Signals, Orders & Positions,
                Backtests, Models & Costs, Risk & Modes, Audit & Settings
  src/styles/   hand-drawn editorial tokens (paper/ink/terracotta/olive)
tests/          76 deterministic tests
docs/           architecture, API contract, runbook
deploy/         Docker Compose, nginx, Kubernetes skeleton
scripts/        dev.ps1 (local session), check.ps1 (tests + UI build)
```

## Configuration

All settings use the `JEV_` prefix and are documented in [`.env.example`](./.env.example).
Highlights:

| Variable | Default | Meaning |
|---|---|---|
| `JEV_TRADING_MODE` | `paper` | `paper`, `shadow`, `testnet`, `live` |
| `JEV_EXECUTION_BACKEND` | `paper` | `paper`, `shadow`, `hummingbot` |
| `JEV_SYNTHETIC_MARKET` | `true` | deterministic market data; UI marks it |
| `JEV_DEMO_MODE` | `true` | synthetic clock advances per cycle |
| `JEV_AUTO_CYCLE_SECONDS` | `0` | background scheduler interval; 0 = manual |
| `JEV_AUTH_DISABLED` | `true` | must be `false` in prod |
| `JEV_MAX_POSITION_NOTIONAL_USDT` | `500` | hard envelope |
| `JEV_MAX_DAILY_LOSS_PCT` | `2` | hard envelope |
| `JEV_REQUIRE_WITHDRAWALS_DISABLED` | `true` | live key policy |
| `JEV_REQUIRE_IP_RESTRICT` | `true` | live key policy |

Production refuses to boot with auth disabled, default API keys, or non-live flags that
contradict `JEV_APP_ENV=prod`.

## Security invariants

1. No secret is logged, returned by the API, written to audit, or rendered in the UI.
2. API keys are stored server-side only; the operator key lives in browser storage.
3. Money and quantity are `Decimal` end to end. SQLite stores canonical decimal strings
   because its `NUMERIC` affinity would otherwise round-trip through binary floats;
   PostgreSQL uses `NUMERIC`.
4. Exchange filters are discovered at runtime. Missing filters or a non-`TRADING` symbol
   halt entries; nothing is hardcoded.
5. Unknown order outcomes become `RECONCILIATION_REQUIRED` — never an assumed fill.
6. OpenCode Go permits only `coding_agent` and `strategy_authoring`; `trade_runtime` and
   `backtest_analysis` are denied in the adapter, not hidden in the UI.
7. A Binance HTTP 451 from this location is surfaced and stops the probe.

## Honest limitations

- **No real exchange traffic has occurred.** Testnet/Hummingbot adapters are Mock- and
  contract-tested only. Their first real use must be an explicitly supervised testnet run.
- **Synthetic market, simulated models.** `SimulatedPolicyGenerator` and
  `SimulatedJevEvaluator` exist to exercise the pipeline. They are labelled everywhere.
- **Backtests are synthetic replays.** The engine is deterministic and discloses its
  assumptions; it is not evidence of edge and does not use historical exchange data.
- **Hummingbot fill reconciliation is partial.** Order state is mapped and unknown states
  force reconciliation; rebuilding a complete fill ledger is a documented follow-up.
- **Testnet does not maintain a local position/PnL ledger yet**; it records order state.
- **The synthetic demo clock advances per cycle**, while policy expiry uses real wall
  clock. This is a demo property, not a production clock design.
- **Single-process deployment.** PostgreSQL + Redis Streams are supported by configuration;
  the split worker topology in the design is not yet packaged.

## Documentation

- [`docs/architecture.md`](./docs/architecture.md) — components, state machines, failure
  behaviour, key decisions.
- [`docs/api.md`](./docs/api.md) — endpoint contract, auth, error shape, examples.
- [`docs/runbook.md`](./docs/runbook.md) — operating paper/testnet/live, incidents.
- [`设计.md`](./设计.md) — the authoritative design document.

## Docker Compose

```bash
docker compose up --build
# API   http://127.0.0.1:8000
# UI    http://127.0.0.1:8080
# Postgres + Redis included; defaults remain paper/synthetic.
```

Never place production keys in `docker-compose.yml`; use secrets or environment injection.

## Kubernetes

`deploy/k8s/` is a skeleton: namespace, ConfigMap, Secret template, API and UI
Deployments/Services, and an Ingress that routes `/api` and `/ws` to the API. It assumes
managed PostgreSQL/Redis, keeps paper/synthetic defaults, and runs the API as a **single
replica** because the scheduler and live-arming token are in-process. See
[`deploy/k8s/README.md`](./deploy/k8s/README.md).
