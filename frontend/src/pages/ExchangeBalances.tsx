import { useState } from "react";

import { EmptyState, ErrorNote, PaperCard, Pill, Stat } from "../components/ui";
import { useApi } from "../hooks/useApi";
import { useI18n } from "../i18n";
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
  const { t } = useI18n();
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
      const routeName = label(route);
      if (!outcome.triggered) {
        return outcome.reason === "cooldown"
          ? t("balances.reconCooldown", {
              route: routeName,
              time: timeAgo(outcome.last_reconcile_at),
              seconds: outcome.cooldown_seconds ?? 0,
            })
          : t("balances.reconNotRun", {
              route: routeName,
              reason: label(outcome.reason ?? "unavailable"),
            });
      }
      if (outcome.error) {
        return t("balances.reconFailed", { route: routeName, error: outcome.error });
      }
      const checked = outcome.checked_orders ?? 0;
      const unknown = outcome.unknown_orders ?? 0;
      const parts = [
        t("balances.reconStatus", {
          route: routeName,
          status: outcome.healthy ? t("balances.healthy") : t("balances.incomplete"),
        }),
        checked === 1
          ? t("balances.reconOneChecked")
          : t("balances.reconChecked", { n: checked }),
      ];
      if (unknown) parts.push(t("balances.reconUnknown", { n: unknown }));
      if (outcome.ledger_quote_after !== undefined) {
        parts.push(
          t("balances.reconLedgerNow", { value: money(outcome.ledger_quote_after) }),
        );
      }
      return parts.join(" · ");
    },
  );

  return (
    <PaperCard
      eyebrow={t("balances.exchangeAccount")}
      title={t("balances.title")}
      actions={
        <button className="button small" onClick={() => void sync()} disabled={busy}>
          {busy ? t("balances.reading") : t("balances.sync")}
        </button>
      }
    >
      <p className="small" style={{ marginBottom: 12, color: "var(--ink-soft)" }}>
        {t("balances.lede")}
      </p>
      {error && <ErrorNote error={error} />}

      {result && (
        <div className={`callout ${anyDrift ? "danger" : "olive"}`} style={{ marginBottom: 12 }}>
          {result.drifts.length === 0
            ? t("balances.noRoute")
            : anyDrift
              ? triggeredReconcile
                ? t("balances.driftTriggered")
                : reconcileCoolingDown
                  ? t("balances.driftCooling")
                  : t("balances.driftRun")
              : t("balances.inTolerance")}
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
                <th>{t("balances.route")}</th>
                <th className="num">{t("balances.venueUsdt")}</th>
                <th className="num">{t("balances.ledgerUsdt")}</th>
                <th className="num">{t("balances.difference")}</th>
                <th>{t("balances.read")}</th>
                <th>{t("balances.state")}</th>
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
                        {withinTolerance ? t("common.inTolerance") : t("common.drift")}
                      </Pill>
                    ) : skip ? (
                      <Pill tone="yellow">{label(skip)}</Pill>
                    ) : (
                      <Pill>{t("common.notRead")}</Pill>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState
          title={t("balances.noRead")}
          hint={t("balances.noReadHint")}
          action={
            <button className="button small" onClick={() => void sync()} disabled={busy}>
              {t("balances.sync")}
            </button>
          }
        />
      )}

      {result && result.drifts.some((d) => Object.keys(d.base_mismatch).length) && (
        <div style={{ marginTop: 14 }}>
          <div className="eyebrow">{t("balances.baseDifferences")}</div>
          <div className="table-wrap" style={{ marginTop: 6 }}>
            <table className="data">
              <thead>
                <tr>
                  <th>{t("balances.route")}</th>
                  <th>{t("balances.asset")}</th>
                  <th className="num">{t("balances.ledger")}</th>
                  <th className="num">{t("balances.venue")}</th>
                  <th className="num">{t("balances.difference")}</th>
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
                note={t("balances.freeLocked", {
                  free: money(snapshot?.free),
                  locked: money(snapshot?.locked),
                })}
              />
            ))}
        </div>
      )}
    </PaperCard>
  );
}
