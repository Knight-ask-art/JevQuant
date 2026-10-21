# Runbook

## Local paper session

```powershell
.\.venv\Scripts\activate
jev doctor
jev serve
# second terminal
cd frontend; npm run dev
```

Optional scheduler: set `JEV_AUTO_CYCLE_SECONDS=60` before starting `jev serve`. Without
it, cycles run only via `POST /api/v1/demo/cycle` or `jev cycle`.

Checklist before trusting a screenshot:

- Ribbon says `PAPER`, `SIMULATED MODELS`, `SYNTHETIC DATA` (local defaults);
- margin notes show database, market, execution, policy and Jev;
- `Audit & Settings → Verify audit chain` returns valid;
- `Risk & Modes` shows the expected limits and no kill switch.

## Moving to Binance Testnet

1. Create a **testnet-only** key pair (https://testnet.binance.vision).
2. Set `JEV_BINANCE_TESTNET_API_KEY` / `JEV_BINANCE_TESTNET_API_SECRET`.
3. Set `JEV_SYNTHETIC_MARKET=false` if real testnet market data is reachable from your
   location. If the public endpoint answers HTTP 451, stop: do not proxy around it.
4. Start the service and call `POST /api/v1/mode/set {"mode":"testnet"}`.
5. Run one cycle and verify:
   - orders carry a real `exchange_order_id`;
   - unknown responses surface as `RECONCILIATION_REQUIRED`, never as fills;
   - a close the venue reported filled but the ledger still had resting is settled by
     `POST /api/v1/execution/reconcile` (position closed, PnL and cash booked);
     balances remain the local projection rather than a pull of the venue account.
6. Exercise `POST /api/v1/execution/reconcile` after a deliberately interrupted submit.

Testnet is an integration environment, not a strategy-validation environment.

## Remote testnet simulation host (Docker Compose)

1. Server: Docker Engine with the compose plugin, 2 GB RAM, outbound HTTPS to
   `testnet.binance.vision`. Images are multi-arch; check `uname -m`.
2. Clone the repository and `cd 新版本` — or upload the deployment archive and run
   `ssh <user>@<host> 'bash -s' < deploy/remote-bootstrap.sh`, which installs Docker,
   extracts the archive, generates the three random secrets into `.env`, builds the
   images sequentially, and starts the stack. It never writes venue keys: fill those
   afterwards and recreate the API container.
3. `cp deploy/server.env.example .env`, then fill:
   - `POSTGRES_PASSWORD`
   - `JEV_ADMIN_API_KEY` / `JEV_INTERNAL_EXECUTION_KEY` (random, distinct)
   - `JEV_BINANCE_TESTNET_API_KEY` / `JEV_BINANCE_TESTNET_API_SECRET`
   - `JEV_PAPER_STARTING_EQUITY` = the testnet account's tUSDT total
4. `docker compose up -d --build`, then `docker compose logs -f api`. The startup
   log must show a healthy database and Redis; the API is not published directly.
5. `ssh -L 8080:127.0.0.1:8080 user@host`, open `http://127.0.0.1:8080`, and paste
   `JEV_ADMIN_API_KEY` into **Audit & Settings**.
6. Before any cycle, run `POST /api/v1/execution/balances/sync`: it must return a
   testnet view. If the database already existed in paper mode, switch the mode
   explicitly; `.env` only seeds the first start.
7. Run one cycle, verify the order carries an `exchange_order_id`, then sync
   balances again and inspect `balance.reconcile_triggered` / `_finished` if the
   venue figure moved.
8. Keep one API replica. Schedulers, the kill switch and the arming token are
   in-process; `pgdata` and `redisdata` are the only durable state outside the
   audit trail.

## Preparing Live (supervised only)

Live is a Hummingbot-backed route and requires a trade-only Binance key with withdrawals
disabled and IP restriction enabled. Never enable withdrawals. Never disable IP
restriction to "make it work".

1. Provision: `JEV_TRADING_MODE=live`, `JEV_ENABLE_LIVE_TRADING=true`,
   `JEV_EXECUTION_BACKEND=hummingbot`, live Binance key, Hummingbot URL/key,
   `JEV_MFA_TOTP_SECRET`, production database and Redis.
2. Run `POST /api/v1/mode/preflight-live`. Every check must pass.
3. Redeem the token with the exact phrase `ARM LIVE TRADING` and a current TOTP code.
4. Confirm the ribbon shows `LIVE · ARMED` on every page.
5. Start with the smallest notional the exchange permits and the server envelope tight.

Arming expires automatically (`JEV_LIVE_ARMING_TTL_SECONDS`). A restart resets the mode to
paper; re-arming is required.

## Stop trading now

```text
POST /api/v1/kill-switch {"reason": "..."}
```

This blocks every new entry immediately. Existing positions keep deterministic exits.
To stop new entries *and* disarm live, also call `POST /api/v1/mode/disarm`.

## Incident: reconciliation required

Symptom: a signal is `RECONCILIATION_REQUIRED`; an order status is unknown.

1. Do **not** resubmit the intent; it is single-use and idempotency-keyed.
2. Inspect the exchange/Hummingbot order by `client_order_id` / order id.
3. If filled, record the fill through the adapter's reconciliation path; if not filled,
   let the order expire/cancel and create a fresh signal on the next cycle.
4. Verify the audit chain after the incident.
5. If the cause is a provider outage, leave entries blocked until health recovers. Do not
   bypass staleness checks.

## Incident: audit chain invalid

1. Treat as tampering or storage corruption: trip the kill switch.
2. Stop the service, snapshot the database, and identify `broken_seq`.
3. Restore from a backup whose chain verifies; never "repair" hashes in place.
4. Record the incident outside the affected database.

## Incident: stale feed / provider outage

Entries block automatically (`market_feed_unhealthy`, `stale_market_data`,
`policy_unavailable`). Exits continue. Confirm dependency margin notes; do not disable
staleness thresholds to force a trade.

## Exchange balance sync

`JEV_BALANCE_SYNC_SECONDS=60` (or `POST /api/v1/execution/balances/sync`) reads the
testnet or Hummingbot account on each tradeable route and stores it in
`balance_snapshots`. It raises `balance.drift` when the quote balance differs by more
than 0.01 USDT or a base-asset quantity differs by more than its step size.

Drift is a signal, not an instruction: the ledger is never rewritten from a balance
difference. Investigate with `POST /api/v1/execution/reconcile`; if that does not
explain the gap, compare the fills manually before touching anything.

## Cost reports

Two read-only reports answer "where did the money go" without touching policy:

```
python -m jev_trading.cost_report --days 30      # realised fees/slippage vs the model
python -m jev_trading.exit_diagnostics --limit 500
```

`cost_report` prices every venue fill (`fee_quote` over the traded notional) and
measures slippage against the snapshot the decision used. Paper fills are excluded:
their commission is the model's own arithmetic echoed back. A fee charged in an asset
with no quote (BNB) is counted as *unpriced*, never as free, and a fill recorded with
no fee at all is excluded from the fee median rather than read as 0 bps — both are
named in the hypotheses.

The report does not change `JEV_FEE_BPS` / slippage settings. Use it to decide, then
change them deliberately.

## Shadow edge evaluator

`GET /api/v1/edge-shadow` returns the recent verdicts. Until a cohort
has `edge_min_samples` fully-elapsed peers, every verdict is
`insufficient_evidence` with `sample_size = 0` — that is the expected state for
a while, not a failure. To find out how far off it is, compare
`counterfactual_rows` with the cohort strings: a large number of rows spread
across many cohorts keeps every cohort thin.

## Schema migrations

`jev migrate` (or `python -m jev_trading.migrations`, or starting the service) applies
pending migrations in order and records them in `schema_migrations`:

| Version | Effect |
|---|---|
| `0001_baseline` | marks a pre-migration database |
| `0002_positions_mode` | adds `positions.mode` (existing rows become `paper`) |
| `0003_fills_external_id` | adds `fills.external_id` + unique (order, trade) index |
| `0004_positions_exit_request` | adds the exit-request bookkeeping columns |
| `0005_counterfactual_path` | adds `mfe_bps` / `mae_bps` / `holding_seconds` / `gap_seconds` / `is_synthetic` to `counterfactual_outcomes` |
| `0006_counterfactual_timing` | adds `time_to_mfe_seconds` / `time_to_mae_seconds` / `round_trip_cost_bps` |
| `0007_fill_fee_amount` | adds `fills.fee_amount` so a fee charged in the base asset or BNB is not lost; existing rows are backfilled from `fee_quote` |
| `0008_edge_shadow` | adds `edge_shadow_decisions` (what the shadow edge evaluator would have decided) |

- `jev migrate --dry-run` lists pending versions without changing anything.
- A database is upgraded in place; the ledger rows are preserved.
- If an applied migration's source changed, startup fails with `checksum_mismatch`.
  Never edit a migration that has run — add a new one.
- Back up the database before migrating in production.

## Restart behaviour

- Mode resets to paper; live arming does not survive restart.
- Risk daily counters live on the account row and survive restart.
- Policies persist and are re-compiled for the active mode; expired policies are ignored.
- Paper orders resting at shutdown remain `ACCEPTED`; reconcile after restart.

## Backup and retention

- Back up PostgreSQL with continuous WAL archiving; audit rows are append-only.
- Redis Streams are a transport, not a ledger. Losing Redis must not lose an approved
  intent, fill or position.
- Retain audit and order events for the regulatory period that applies to you.

## Containers and Kubernetes

`docker compose up --build` gives Postgres, Redis, the API and the static UI with paper
and synthetic defaults. For a cluster, `deploy/k8s/` provides the object skeleton; see
`deploy/k8s/README.md` for the apply sequence and the assumptions it makes.

- Run **one API replica**. The scheduler and the live-arming token are in-process; a
  second replica would run a second scheduler against the same account.
- Keep the ConfigMap non-secret and paper/synthetic-safe. Arming live is a runtime
  handshake, never a manifest edit.
- The Ingress must pass `/ws` upgrades; the shipped annotations raise proxy timeouts for
  the event stream.

## Environment hygiene

- `.env` is git-ignored. Never commit keys.
- `JEV_AUTH_DISABLED=true` is localhost-only; production refuses to boot with it.
- Rotate the operator key and internal execution key independently.
- Review `GET /api/v1/system/config` before any live session; it is redacted by design.
