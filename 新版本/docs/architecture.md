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
   - positions/PnL are **not** assumed — the testnet position ledger is a known gap.
6. Exercise `POST /api/v1/execution/reconcile` after a deliberately interrupted submit.

Testnet is an integration environment, not a strategy-validation environment.

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

This blocks every new entry immediately. Existing positions keep deterministic exits
on the route that opened them. To stop new entries *and* disarm live, also call
`POST /api/v1/mode/disarm`.

A mode change (`POST /api/v1/mode/set`) is refused with 409 while a live position is
open; close the live position first. After a restart the mode is paper, but any live
position still exits through Hummingbot.

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

## Exchange filters

Filters (LOT_SIZE, PRICE_FILTER, MIN_NOTIONAL) are always discovered at runtime, never
hardcoded. The cache is bounded, and **entry paths** only trust filters younger than
`JEV_ENTRY_FILTER_TTL_SECONDS` (default 60 s): an older value forces a re-query, and a
failed re-query blocks that entry. A venue halting a symbol therefore cannot be traded on
a cached `TRADING` status for more than that window. Exit paths may use the longer cache
so they keep working during a brief `exchangeInfo` outage.

## Environment hygiene

- `.env` is git-ignored. Never commit keys.
- `JEV_AUTH_DISABLED=true` is localhost-only; production refuses to boot with it.
- Rotate the operator key and internal execution key independently.
- Review `GET /api/v1/system/config` before any live session; it is redacted by design.
