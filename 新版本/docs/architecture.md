# Architecture

## Purpose

Jev Trading separates four kinds of authority:

| Authority | Component | Cannot do |
|---|---|---|
| Intent authoring | LLM / simulated author | exceed the server envelope, create orders, choose size |
| Evaluation | Jev System One | author policies, size orders, touch keys |
| Approval | deterministic risk engine | call a model, trust claimed values |
| Execution | adapter for the active mode | invent approval, change an approved intent |

The system is fail-closed: every missing or uncertain input blocks **new entries**.
Existing positions continue through deterministic exits while execution is healthy.

## Runtime topology (current)

```text
FastAPI control plane
  └── AppContainer (single composition root)
        ├── Database (SQLite locally, PostgreSQL in staging/prod)
        ├── EventBroker (in-process fan-out, optional Redis Streams)
        ├── MarketService ──► synthetic provider | Binance public REST
        ├── PolicyService ──► compiler ──► policy rows + audit
        ├── PolicyGenerator ──► OpenCode Console | simulated
        ├── JevAdapter ──► Jev System One | simulated
        ├── RiskEngine
        ├── Execution adapters: paper, shadow, testnet, hummingbot
        ├── ModeManager (mode, kill switch, live arming)
        ├── AuditService (hash chain, redaction)
        └── TradingOrchestrator (per-cycle pipeline)
```

The design's split worker topology (feature/model/risk/execution/reconciliation workers)
maps onto the same components; the current build runs them in one process. PostgreSQL and
Redis are configured through `JEV_DATABASE_URL` / `JEV_REDIS_URL`; Redis is optional
locally and never holds the only copy of an approved intent.

## Cycle pipeline

`TradingOrchestrator.run_cycle`:

1. Expire stale policies.
2. Load the active compiled policy (or ask the author for one).
3. Fetch snapshots for the policy universe plus any symbol with an open position;
   persist each snapshot.
4. Compute the account snapshot (equity, gross exposure, drawdown, daily realized).
5. Evaluate dependency health and build the `SystemSnapshot`.
6. Decide the entries-blocked reason, in precedence order:
   `kill_switch` → `policy_unavailable` → `market_feed_unhealthy` → `execution_unhealthy`.
7. For each symbol policy: create a `CANDIDATE`, ask Jev typed questions, persist the
   evaluation, transition to `SCORED`, apply the gate, then either `REJECTED` or
   `ELIGIBLE`.
8. For an eligible signal: exchange filters → account/position/cooldown checks →
   idempotency key → notional `min(symbol cap, 25% available)` → provisional intent →
   risk snapshot hash → persist intent → risk decision → `APPROVED` or `RISK_REJECTED`.
9. Submit the approved intent through the adapter for the mode; apply the receipt through
   the signal state machine.
10. Manage exits independently of entries: stop/take/time reasons, unsettled-close guard,
    close intent, risk check (exits skip entry-only gates), submit, apply receipt.
    Exits are routed by the mode that **opened** the position, not the currently
    selected mode, so a live position is still closed through Hummingbot after a
    restart (which resets the mode to paper). A close left `EXIT_PENDING` is resolved
    each cycle: filled closes the signal, rejected/ERROR returns it to `POSITION_OPEN`,
    and a close still working after `CLOSE_ORDER_WORKING_LIMIT_SECONDS` is cancelled.
    The kill switch may defer exits that cross routes, never same-route exits.
11. Cycles are serialized by a container lock. The scheduler skips an interval when a
    cycle is still running; a manual `POST /api/v1/demo/cycle` returns 409 instead of
    queueing behind it.
11. Write a PnL snapshot, audit `cycle.completed`, publish to the event stream.

The optional scheduler calls step 1–11 on `JEV_AUTO_CYCLE_SECONDS`. A scheduler error is
audited and the loop continues; it does not silently stop.

## Signal lifecycle

```text
CANDIDATE → JEV_EVALUATING → SCORED → ELIGIBLE → INTENT_CREATED → RISK_PENDING
                                                     │
                                                     ├── RISK_REJECTED
                                                     ▼
                                                  APPROVED
                                                     ▼
                                             EXECUTION_PENDING
                                                     ▼
                               ACCEPTED → PARTIALLY_FILLED → FILLED → POSITION_OPEN
                                                     │
                                                     ├── CANCELLED
                                                     ├── RECONCILIATION_REQUIRED
                                                     └── ERROR

POSITION_OPEN → EXIT_PENDING → CLOSED
```

Transitions are validated by `assert_signal_transition`; an illegal transition raises.
Unknown execution outcomes never invent a fill.

## Intent and approval

- `TradeIntent` is frozen, has a ≤60 s TTL and carries every execution-relevant value.
- `risk_snapshot_hash` covers the policy, account, market, filters, system state and
  position; the risk engine recomputes it and rejects a mismatch.
- `RiskEngine.evaluate` returns a `RiskDecision`. Only `ApprovedTradeIntent` crosses into
  an adapter; it adds the effective notional, approved quantity/price and fee estimate.
- Idempotency keys are unique. The entry key is
  `intent-{policy8}-{symbol}-{candle_epoch}-buy`.
- The execution gateway can dispatch an already-approved intent via
  `POST /api/v1/execution/intents`; it verifies status, expiry, mode and the persisted
  risk decision before calling the adapter.

## The policy compiler

The LLM output is untrusted. The compiler:

- rejects policies that do not cover the active mode, are expired, or are from the future;
- removes symbols outside the server allowlist and sides outside the spot long-only
  envelope;
- clamps expiry to the server TTL;
- clamps per-symbol notional/spread/slippage and raises cooldown to the server minimum;
- clamps portfolio exposure, positions and loss/drawdown caps;
- raises gate thresholds to server floors;
- clamps stop/take/holding into the server range;
- records every adjustment as a `Clamp` for the Strategy Desk and audit.

An empty universe after compilation is a hard validation error.

## Exit feasibility pre-check

Before an entry is approved, risk simulates an adverse exit:

```text
exit_price = entry_price × (1 − (stop_loss_bps + taker_fee_bps + exit_slippage_bps) / 10000)
```

If the resulting quantity or notional cannot satisfy `LOT_SIZE` / `MIN_NOTIONAL`, the
intent is rejected with `REJECT_EXIT_MIN_NOTIONAL`. A position the system could not legally
sell is never opened.

## Modes and live arming

| Mode | Adapter | Requirements |
|---|---|---|
| Paper | `PaperExecutionAdapter` | none |
| Shadow | `ShadowExecutionAdapter` | records `SHADOWED`, no position |
| Testnet | `BinanceTestnetExecutionAdapter` | testnet credentials |
| Live | `HummingbotExecutionAdapter` | env flags + full preflight + arming |

Live arming is atomic and does not survive a restart (the mode manager resets to paper on
startup). Preflight checks: env mode, live flag, Hummingbot backend, MFA secret, Binance
trade-only key, kill switch off, Redis health (if configured), database, feed, Hummingbot
health, and Binance permissions (reading, spot trading, withdrawals disabled, IP restrict).
A passing preflight issues a short-lived single-use token; redeeming it requires the exact
phrase and a valid TOTP code.

Disarming and the kill switch keep the current mode so existing positions retain their
execution path. They only stop new entries.

## Persistence and precision

PostgreSQL is the intended authoritative store. SQLite is the local default.

All economically significant values are `Decimal`. `DecimalText` stores canonical decimal
strings on SQLite (its `NUMERIC` affinity would use binary floats) and `NUMERIC(28,12)` on
PostgreSQL. The repository never computes financial values in SQL.

Audit events are hash-linked (`prev_hash` + canonical JSON body + `event_hash`). Secret-
shaped keys are redacted recursively before persistence. Verification returns the first
broken sequence number.

## Exchange balance sync

Testnet and live routes expose `fetch_balances()`; `BalanceSyncService` stores the
venue view in `balance_snapshots`, audits `balance.synced` / `balance.drift` and never
edits the ledger. Tolerances: 0.01 USDT on the quote balance, the asset step size on
base quantities. A route that cannot be read (unconfigured, transport error) is
skipped and audited as `balance.sync_failed`, never assumed. The scheduler runs on
`JEV_BALANCE_SYNC_SECONDS`; `POST /api/v1/execution/balances/sync` runs it on demand.

## Failure behaviour

| Failure | New entries | Existing exits | Notes |
|---|---|---|---|
| Market feed stale/unhealthy | blocked | continue | entry-only `stale_market_data` / `feed_unhealthy` |
| Policy missing/expired | blocked | continue | `policy_unavailable` |
| Jev timeout/invalid answer | blocked for that signal | continue | signal `ERROR`, fail closed |
| LLM unavailable | blocked until policy exists | continue | existing policy stays active |
| Exchange rules missing/not TRADING | blocked | blocked for that symbol | never hardcode filters; entries refresh filters older than `JEV_ENTRY_FILTER_TTL_SECONDS` |
| DB unavailable | blocked | blocked | readiness fails |
| Redis unavailable | blocked **only if configured** | continue | in-process bus still delivers |
| Execution adapter unhealthy | blocked | blocked | `execution_unhealthy` |
| Unknown order outcome | blocked for that signal | manual reconciliation | `RECONCILIATION_REQUIRED` |
| Venue balance differs from ledger | unchanged | unchanged | audit `balance.drift`; sync never edits the ledger |
| Kill switch | blocked | continue | mode unchanged |
| Live not armed | blocked | continue | only relevant in live mode |

## Deliberate decisions

1. **Console vs Go.** Runtime strategy authoring uses OpenCode Console with purpose
   `trade_runtime`. OpenCode Go is restricted to coding-agent/authoring traffic; the adapter
   enforces the purpose allowlist server-side.
2. **One execution contract.** Paper is not a toy path: it consumes the same approved
   intent, applies filters, fees, slippage and deterministic partial fills, and writes the
   same ledger rows.
3. **Simulation is visible, not hidden.** Synthetic candles, simulated models, simulated
   orders and synthetic backtests carry explicit flags into API responses, provenance,
   audit and UI badges.
4. **Manual cycle in live is refused.** Live cycles belong to the configured scheduler so
   ad-hoc UI clicks cannot become an execution channel.
