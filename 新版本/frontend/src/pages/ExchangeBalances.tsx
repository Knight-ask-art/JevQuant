import { useState } from "react";

import { EmptyState, ErrorNote, PaperCard, Pill, Stat } from "../components/ui";
import { useApi } from "../hooks/useApi";
import { ApiError, api } from "../lib/api";
import { label, money, signed, timeAgo } from "../lib/format";

interface BalanceSnapshot {
  source: string;
  asset: string;
  free: string;
  locked: string;
  total: string;
  is_simulated: boolean;
  ledger_quote: string | null;
  created_at: string;
}

interface BalanceDrift {
  route: string;
  quote_asset: string;
  ledger_quote: string;
  venue_quote: string;
  difference: string;
  within_tolerance: boolean;
  base_mismatch: Record<string, { ledger: string; venue: string; difference: string }>;
}

interface ReconcileOutcome {
  triggered: boolean;
  reason?: string;
  healthy?: boolean;
  error?: string;
  checked_orders?: number;
  unknown_orders?: number;
  notes?: string[];
  difference?: string;
  ledger_quote_after?: string;
  last_reconcile_at?: string;
  cooldown_seconds?: number;
}

interface SyncResult {
  drifts: BalanceDrift[];
  skipped: Record<string, string>;
  reconciled?: Record<string, ReconcileOutcome>;
  in_tolerance: boolean;
}

const ROUTES = ["testnet", "live"] as const;

export function ExchangeBalances() {
  const snapshots = useApi<{ items: BalanceSnapshot[] }>("/api/v1/execution/balances", 15000);
  const [result, setResult] = useState<SyncResult | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);

  async function sync() {
    setBusy(true);
    setError(null);
    try {
      setResult(await api.post<SyncResult>("/api/v1/execution/balances/sync"));
      await snapshots.reload();
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setBusy(false);
    }
  }

  // The sync response is authoritative for the last run; the stored snapshots
  // (written by the scheduler too) are the durable record.
  const byRoute = new Map<string, BalanceSnapshot[]>();
  for (const item of snapshots.data?.items ?? []) {
    byRoute.set(item.source, [...(byRoute.get(item.source) ?? []), item]);
  }
  const quoteDrift = new Map(result?.drifts.map((d) => [d.route, d]) ?? []);
  const routeRows = ROUTES.map((route) => {
    const snapshot = byRoute.get(route)?.[0];
    const drift = quoteDrift.get(route);
    const difference = drift
      ? Number(drift.difference)
      : snapshot && snapshot.ledger_quote !== null
        ? Number(snapshot.total) - Number(snapshot.ledger_quote)
        : null;
    const withinTolerance = drift
      ? drift.within_tolerance
      : difference === null
        ? null
        : Math.abs(difference) <= 0.01;
    return { route, snapshot, drift, difference, withinTolerance, skip: result?.skipped[route] };
  });
  const configured = routeRows.some((row) => row.snapshot);
  const anyDrift = result ? !result.in_tolerance : false;
  const triggeredReconcile = Object.values(result?.reconciled ?? {}).some(
    (outcome) => outcome.triggered,
  );
  const reconcileCoolingDown = Object.values(result?.reconciled ?? {}).some(
    (outcome) => outcome.reason === "cooldown",
  );
  const reconcileLines = Object.entries(result?.reconciled ?? {}).map(
    ([route, outcome]) => {
      if (!outcome.triggered) {
        return outcome.reason === "cooldown"
          ? `${label(route)}: reconciliation ran ${timeAgo(outcome.last_reconcile_at)} (cooldown ${outcome.cooldown_seconds}s)`
          : `${label(route)}: reconciliation not run — ${outcome.reason ?? "unavailable"}`;
      }
      if (outcome.error) {
        return `${label(route)}: reconciliation failed (${outcome.error}) — ledger untouched`;
      }
      const checked = outcome.checked_orders ?? 0;
      const unknown = outcome.unknown_orders ?? 0;
      const parts = [
        `${label(route)}: reconciliation ${outcome.healthy ? "healthy" : "incomplete"}`,
        `${checked} order${checked === 1 ? "" : "s"} checked`,
      ];
      if (unknown) parts.push(`${unknown} unknown`);
      if (outcome.ledger_quote_after !== undefined) {
        parts.push(`ledger now ${money(outcome.ledger_quote_after)} USDT`);
      }
      return parts.join(" · ");
    },
  );

  return (
    <PaperCard
      eyebrow="Exchange account"
      title="Venue balances & drift"
      actions={
        <button className="button small" onClick={() => void sync()} disabled={busy}>
          {busy ? "Reading venue…" : "Sync balances"}
        </button>
      }
    >
      <p className="small" style={{ marginBottom: 12, color: "var(--ink-soft)" }}>
        The exchange's view of the account is recorded next to the ledger's figure. Drift is
        reported for reconciliation — it never rewrites cash or positions. A drift triggers one
        reconciliation for that route, rate-limited so a persistent difference cannot hammer the
        venue.
      </p>
      {error && <ErrorNote error={error} />}

      {result && (
        <div className={`callout ${anyDrift ? "danger" : "olive"}`} style={{ marginBottom: 12 }}>
          {result.drifts.length === 0
            ? "No route returned a balance: nothing is configured, so nothing is claimed."
            : anyDrift
              ? triggeredReconcile
                ? "Drift outside tolerance — reconciliation was triggered for the affected route; check its notes below."
                : reconcileCoolingDown
                  ? "Drift outside tolerance — reconciliation ran recently; the per-route cooldown is still active."
                  : "Drift outside tolerance — run reconciliation before trusting the ledger."
              : "Venue and ledger agree within tolerance."}
          {reconcileLines.length > 0 && (
            <div className="small" style={{ marginTop: 6 }}>
              {reconcileLines.map((line, index) => (
                <div key={`${index}-${line}`}>{line}</div>
              ))}
            </div>
          )}
        </div>
      )}

      {routeRows.some((row) => row.snapshot || row.drift || row.skip) ? (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Route</th>
                <th className="num">Venue USDT</th>
                <th className="num">Ledger USDT</th>
                <th className="num">Difference</th>
                <th>Read</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {routeRows.map(({ route, snapshot, drift, difference, withinTolerance, skip }) => (
                <tr key={route}>
                  <td>
                    <strong>{label(route)}</strong>
                  </td>
                  <td className="num">{money(drift ? drift.venue_quote : snapshot?.total)}</td>
                  <td className="num">
                    {money(drift ? drift.ledger_quote : snapshot?.ledger_quote)}
                  </td>
                  <td className="num">{difference === null ? "—" : signed(difference)}</td>
                  <td className="muted small">
                    {timeAgo(drift ? new Date().toISOString() : snapshot?.created_at)}
                  </td>
                  <td>
                    {withinTolerance !== null ? (
                      <Pill tone={withinTolerance ? "olive" : "danger"} dot>
                        {withinTolerance ? "in tolerance" : "drift"}
                      </Pill>
                    ) : skip ? (
                      <Pill tone="yellow">{skip}</Pill>
                    ) : (
                      <Pill>not read</Pill>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState
          title="No venue balance read yet."
          hint="Configure testnet or live credentials, then sync. Paper and shadow routes have no venue account."
          action={
            <button className="button small" onClick={() => void sync()} disabled={busy}>
              Sync balances
            </button>
          }
        />
      )}

      {result && result.drifts.some((d) => Object.keys(d.base_mismatch).length) && (
        <div style={{ marginTop: 14 }}>
          <div className="eyebrow">Base-asset differences</div>
          <div className="table-wrap" style={{ marginTop: 6 }}>
            <table className="data">
              <thead>
                <tr>
                  <th>Route</th>
                  <th>Asset</th>
                  <th className="num">Ledger</th>
                  <th className="num">Venue</th>
                  <th className="num">Difference</th>
                </tr>
              </thead>
              <tbody>
                {result.drifts.flatMap((drift) =>
                  Object.entries(drift.base_mismatch).map(([asset, row]) => (
                    <tr key={`${drift.route}-${asset}`}>
                      <td>{label(drift.route)}</td>
                      <td className="mono">{asset}</td>
                      <td className="num">{row.ledger}</td>
                      <td className="num">{row.venue}</td>
                      <td className="num">{signed(row.difference, 8)}</td>
                    </tr>
                  )),
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {configured && !result && (
        <div className="row" style={{ marginTop: 12, gap: 24 }}>
          {routeRows
            .filter((row) => row.snapshot)
            .map(({ route, snapshot }) => (
              <Stat
                key={route}
                label={`${label(route)} · ${snapshot?.asset ?? "USDT"}`}
                value={money(snapshot?.total)}
                note={`free ${money(snapshot?.free)} · locked ${money(snapshot?.locked)}`}
              />
            ))}
        </div>
      )}
    </PaperCard>
  );
}
