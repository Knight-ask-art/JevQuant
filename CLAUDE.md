# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

JevQuant is an LLM-authored, risk-gated trading runtime. A model writes a strategy policy;
**Jev** scores each closed candle into a typed `buy` / `reject` / `wait` decision; an
independent risk engine — never the model — decides what actually reaches the exchange.
Execution runs through one interface across Paper, Shadow, Binance Testnet, and Live.

- Package: `src/jev_trading/` (Python 3.12+, FastAPI + SQLAlchemy 2 async + React/TS UI).
- The authoritative design documents are in `docs/` (`architecture.md`, `api.md`,
  `runbook.md`) and `设计.md`. Prefer them over this file when they disagree.
- Production host is a single Docker Compose box (`deploy/`); see `docs/runbook.md`.

## Core Invariants

1. **The model advises; the risk engine decides.** A policy from the LLM is untrusted input
   and goes through the compiler, which can only *narrow* server limits
   (`max(policy, server)`). Jev cannot create orders, size positions, or touch keys.
2. **Decimal only.** Every price, quantity, notional and fee is `decimal.Decimal`. Binary
   floats never reach order sizing or limits.
3. **Fail closed.** Missing exchange rules, a non-`TRADING` symbol, a stale feed, an
   unhealthy adapter, an unknown order outcome, or a missing policy all block new entries.
   Market filters are always queried, never hardcoded.
4. **Exit feasibility before entry.** A buy is rejected (`REJECT_EXIT_MIN_NOTIONAL`) if an
   adverse move minus fees/slippage would leave a position that cannot legally be sold.
5. **No secret leaks.** Never log, return, or persist API keys or authorization headers.
   Model keys are write-only in the UI; venue keys live only in the environment.
6. **Live is gated.** Live needs the mode, `JEV_ENABLE_LIVE_TRADING`, a full preflight and
   an arming token with a TTL. Manual cycles are refused in live. Default is `paper`.
7. **Auth is intentionally off** (`JEV_AUTH_DISABLED=true`): the host is reachable only
   through an SSH tunnel. Do not "fix" this by turning auth on.

## Development Commands

```powershell
# Interpreter (Python 3.12 virtualenv)
.\.venv\Scripts\python.exe -m pytest

# One file / one test
.\.venv\Scripts\python.exe -m pytest tests/test_counterfactual.py -v
.\.venv\Scripts\python.exe -m pytest tests/test_counterfactual.py -k "stop" -v

# CLI (src-layout: set PYTHONPATH when not installed)
$env:PYTHONPATH="src"
.\.venv\Scripts\python.exe -m jev_trading.cli.main --help
# subcommands: doctor, migrate, demo, cycle, status, serve, backtest

# Migrations (forward-only, checksum-verified)
.\.venv\Scripts\python.exe -m jev_trading.cli.main migrate --dry-run
```

`tests/conftest.py` holds the shared fixtures (`compiled_policy`, `market_snapshot`,
`symbol_rules`, `NOW`). Tests use SQLite (`aiosqlite`) so they need no external services.

## Architecture

`src/jev_trading/`

- `config.py` — `Settings` from the environment, mode validation, live preflight guards.
  `redacted_summary()` is what the API is allowed to return.
- `domain/` — `money.py` (Decimal helpers), `intent.py` (`TradeIntent`,
  `risk_snapshot_hash`), `risk.py`, `policy.py` (`EffectivePolicy`, exit defaults),
  `enums.py` (signal states + legal transitions), `errors.py` (`AppError`/`ErrorCode`).
- `storage/` — `db.py` (engine/session), `models.py` (SQLAlchemy rows), `repository.py`
  (all queries). Migrations live in `migrations.py` and run at container start.
- `services/orchestrator.py` — `run_cycle`, the whole pipeline: policy → snapshots →
  per-symbol Jev evaluation → gate → intent → risk → dispatch → exits → PnL.
- `services/regime_gate.py` — deterministic cost gate: expected move over the policy's
  holding window vs the round trip. Records its verdict on every signal; can refuse one.
- `domain/costs.py` — the one definition of a round trip (fees + impact), and the
  pricing of a venue commission into the quote asset.
- `services/cost_report.py` — realised fees/slippage from the fills, next to the model.
  Read-only; see `docs/architecture.md`.
- `services/counterfactual.py` — replays **blocked** buys over 1m bars so the gate can be
  judged on outcomes. Read-only and best-effort; see `docs/architecture.md`.
- `services/experiment.py` + `experiment.py` — offline A/B/C comparison runner
  (portfolio vs signal level, hold-cash and SMA baselines, MFE/MAE).
- `services/container.py` — dependency wiring, scheduler loop, dependency probes.
- `services/policy_service.py` — authoring + the compiler that narrows LLM output.
- `services/agent_tools.py` — the read-only tools the authoring agent may call.
- `services/audit.py` — hash-chained audit log (`audit_events`); never break the chain.
- `adapters/market/` — `binance.py` (public REST, paged klines), `synthetic.py` (demo).
- `adapters/execution/` — `paper.py`, `shadow.py`, `binance_testnet.py`, `hummingbot.py`.
  All four satisfy the same contract; only paper/testnet are exercised in CI.
- `api/` — FastAPI app, routes, auth dependency, WebSocket event stream.
- `cli/main.py` — `jev` entry point (`serve`, `cycle`, `migrate`, `status`, …).
- `frontend/` — React 18 + TS + Vite cockpit, served by nginx behind the API.

## Conventions

- **Money** — construct from strings (`Decimal("0.1")`), never from floats.
- **Cycles** — one Jev call per symbol per closed candle; pacing is checked before the
  signal is created. Never re-ask Jev for a candle that was already evaluated.
- **Idempotency** — entry key `intent-{policy8}-{symbol}-{candle_epoch}-buy`; one trade
  per candle is enforced at intent level too, not just by the key.
- **Simulation is labelled** — synthetic candles, simulated models, simulated orders and
  synthetic backtests carry explicit flags into the API, audit and UI.
- **Migrations** — never edit one that has run (checksum mismatch fails startup); add a new
  one and register it in `MIGRATIONS`.
- **Reporting** — timestamps are stored UTC; report them to the user in UTC+8.

## Testing Notes

- `pytest` must stay green; the suite is the only executable description of behaviour.
- Prefer testing the pure functions (`simulate()`, `_gate_bucket()`, the compiler) and the
  repository queries directly over driving a whole cycle.
- A fix for a real defect should come with a test that fails without it.
