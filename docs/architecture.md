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
   evaluation, transition to `SCORED`, apply the Jev gate and then the regime gate, then
   either `REJECTED` or `ELIGIBLE`.
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
12. Replay blocked buys whose window has closed (`counterfactual_outcomes`; read-only,
    best-effort, bounded per cycle).
13. Write a PnL snapshot, audit `cycle.completed`, publish to the event stream.

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
- Dispatch re-runs the risk engine against a **fresh** snapshot and re-derives
  `risk_snapshot_hash`. A price that moved past what was approved fails the check
  (`execution.recheck_failed`) rather than being filled at the new price.
- Jev is asked at most once per symbol per closed candle. Pacing is checked before the
  signal is created and keyed on the states that mean "Jev already saw this candle"
  (`JEV_EVALUATING` onward), so a restart or a rejected signal does not re-bill the call.
  A budget refusal is not a Jev call either: the signal goes `EXPIRED`, not `ERROR`.
- An intent-level guard refuses a second trade on a candle that already has one
  (`candle_already_traded`), independent of the entry idempotency key.

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

## Regime gate

The Jev gate answers "is this candidate good"; it says nothing about whether the move
available over the holding window is bigger than the round trip paid to capture it.
`services/regime_gate.py` asks that second question and can only refuse:

```text
expected_abs_move_bps = atr_5m_bps * sqrt(horizon_minutes / 5)
required_move_bps     = (2 x fee + entry slippage + exit slippage) x cost_multiplier
blocked               = expected < required   (or no ATR at all)
```

- It runs after Jev scoring, on every signal that clears the Jev gate, and its verdict is
  merged into the same `detail` the signal carries. A gate that ran first would leave no
  outcome to judge it by.
- `regime_gate_enforce` starts `false`. In shadow the trade still happens and the verdict
  is audited as `regime.shadow_block`; with it on, the signal goes `REJECTED` with
  `stage="regime_gate"` before any intent exists.
- A missing or zero `atr_5m_bps` fails closed (`no_volatility_estimate`): the feature is
  `0.0` when the ATR could not be computed, and unknown volatility is an unanswered
  question, not a reason to trade.
- `counterfactual_outcomes.blocked_rule` records `regime_expected_move_below_cost` for a
  signal the regime gate refused, so the two gates are judged on separate cohorts. The
  verdict itself is read back from the signal event, because ATR is not on the score.

## Counterfactual outcomes

A blocked entry is only evidence about the gate if its outcome is measured. After the
cycle's own work is done, `CounterfactualService.evaluate_pending` replays blocked buys
that have played out and stores one row per signal in `counterfactual_outcomes`.

- Window: 1m bars with `open_time > signal.created_at` and `close_time <= horizon_end`,
  where `horizon_end = created_at + max_holding_seconds`. The entry minute is never used
  for the exit, so a bar that contains the entry cannot be mistaken for a profitable one.
- Pessimistic: a bar touching both stop and target is a stop; both taker fees and exit
  slippage are charged; `method` records the model (`1m-conservative-v1`).
- `mfe_bps` / `mae_bps` record how far price ever went each way, so "the gate was right"
  and "the exit geometry was wrong" can be told apart.
- Coverage is bounded by the longest holding window a policy may carry plus a margin
  (`replay_lookback`); a lookback shorter than that horizon would drop every long-horizon
  signal permanently. A signal older than the bound is not recorded at all.
- A window with no bars is stored as `exit_reason="no_data"` so it is never rescanned.
- The per-cycle budget is `MAX_REPLAYS_PER_CYCLE` (6) **attempted** replays, and only
  `entry_choice == "buy"` is queued. A signal that cannot produce a row — not yet past its
  horizon, or its policy/snapshot is gone — is skipped without consuming a slot; otherwise
  a run of them starves the signals behind them, which is what happened in production.
- Replay is read-only and best-effort: a failure is audited as `counterfactual.error`
  and retried next cycle; it never affects trading.

### Exit diagnostics

`services/exit_diagnostics.py` reads the recorded outcomes and names a hypothesis rather
than proposing a parameter. It separates two readings that a plain P&L cannot:

- a timeout whose MFE never covered its round-trip cost is `market_never_offered_the_trade`
  — no exit rule could have saved it;
- a timeout that peaked above 40 bps and still timed out is
  `profit_was_available_and_given_back` — that is an exit-geometry question.

It also reports `capture_ratio` (realised gross over MFE), giveback, the median cost, and
`time_to_mfe_seconds`. Run it with `python -m jev_trading.exit_diagnostics`.

`GET /api/v1/...`-style access is via the `GET_BLOCKED_SIGNALS` agent tool, which reports
counts, average `net_bps`, per-rule attribution and timeout-path MFE.

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
base quantities. Base comparison is limited to the allowed symbol universe:
faucet or airdrop tokens the ledger cannot hold are recorded in the venue view but
never reported as ledger drift. A route that cannot be read (unconfigured, transport
error) is
skipped and audited as `balance.sync_failed`, never assumed. The scheduler runs on
`JEV_BALANCE_SYNC_SECONDS`; `POST /api/v1/execution/balances/sync` runs it on demand.

A drift triggers one `reconcile()` for that route — reconciliation is the only path
that may settle the difference, from venue-confirmed fills. The trigger is audited as
`balance.reconcile_triggered` / `balance.reconcile_finished` (or
`balance.reconcile_failed`, storing only the error type) and is rate limited per route
by `JEV_BALANCE_RECONCILE_COOLDOWN_SECONDS`; the cooldown starts even on failure, so
a persistent difference cannot hammer an unhealthy venue. `JEV_BALANCE_AUTO_RECONCILE=false`
disables the trigger. Concurrent syncs (API and scheduler) are serialized, so one
drift is not reconciled twice, and reconciles take the container's ledger lock so a
reconcile cannot interleave with a trading cycle's position writes. The sync response
reports the outcome per route (`reconciled`), including a suppressed trigger.

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
| Venue balance differs from ledger | unchanged | unchanged | audit `balance.drift`; one auto-reconcile per route (cooldown) may settle venue-confirmed fills |
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
