import { useState } from "react";
import { Link } from "react-router-dom";

import { EquityChart } from "../components/charts";
import { ModelSettingsModal } from "../components/ModelSettingsModal";
import { EmptyState, ErrorNote, PaperCard, Pill, ScribbleUnderline, Stat } from "../components/ui";
import { useApi } from "../hooks/useApi";
import { useI18n } from "../i18n";
import { ApiError, api } from "../lib/api";
import { clock, countdown, label, money, pct, shortId, signed, timeAgo } from "../lib/format";
import type { OrderView, PnlPoint, SystemStatus } from "../lib/types";

const INTERVALS = [60, 300, 900, 1800, 3600];

export function Cockpit() {
  const { t } = useI18n();
  const status = useApi<SystemStatus>("/api/v1/system/status", 5000);
  const pnl = useApi<{ items: PnlPoint[] }>("/api/v1/pnl?limit=120", 15000);
  const orders = useApi<{ items: OrderView[] }>("/api/v1/orders?limit=6", 10000);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [modelsOpen, setModelsOpen] = useState(false);

  const view = status.data;
  const mode = view?.mode;
  const account = view?.account;
  const automation = view?.automation;
  const models = view?.models;
  const currentMode = mode?.mode ?? "paper";
  const points = (pnl.data?.items ?? [])
    .slice()
    .reverse()
    .map((point) => ({ at: point.as_of, equity_usdt: point.equity_usdt }));

  async function act(name: string, work: () => Promise<unknown>) {
    setBusy(name);
    setError(null);
    try {
      await work();
      await Promise.all([status.reload(), pnl.reload(), orders.reload()]);
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setBusy(null);
    }
  }

  function setMode(next: "paper" | "testnet") {
    return act(next, () => api.post("/api/v1/mode/set", { mode: next }));
  }

  function runNow() {
    return act("cycle", () => api.post("/api/v1/demo/cycle"));
  }

  function setAutomation(enabled: boolean, intervalSeconds?: number) {
    return act("automation", () =>
      api.put("/api/v1/settings/automation", {
        enabled,
        interval_seconds: intervalSeconds ?? automation?.interval_seconds ?? 300,
      }),
    );
  }

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="page-head">
        <div>
          <div className="page-kicker">
            {t("cockpit.kicker", { time: clock(view?.server_time) })}
          </div>
          <h1>{t("cockpit.title")}</h1>
          <ScribbleUnderline width={340} />
          <p className="page-lede">{t("cockpit.lede")}</p>
        </div>
        <div className="row">
          {mode?.kill_switch && <Pill tone="danger">{t("ribbon.entriesStopped")}</Pill>}
          <button
            className="button"
            onClick={() => void runNow()}
            disabled={busy !== null}
          >
            {busy === "cycle" ? t("common.running") : t("cockpit.runNow")}
          </button>
        </div>
      </div>

      {error && <ErrorNote error={error} />}

      <div className="mode-switch">
        {(["paper", "testnet"] as const).map((value) => (
          <button
            key={value}
            className={`mode-tile${currentMode === value ? " active" : ""}`}
            onClick={() => void setMode(value)}
            disabled={busy !== null || currentMode === value}
          >
            <strong>{value === "paper" ? t("cockpit.paper") : t("cockpit.testnet")}</strong>
            <span>
              {value === "paper" ? t("cockpit.paperHint") : t("cockpit.testnetHint")}
            </span>
          </button>
        ))}
        <div className="mode-note">
          <span className="small muted">
            {t("cockpit.mode")}: {label(currentMode)}
          </span>
          <Link className="small" to="/risk">
            {t("cockpit.liveNote")}
          </Link>
        </div>
      </div>

      <div className="cockpit-grid">
        <div className="stack">
          <PaperCard
            eyebrow={t("cockpit.trend")}
            title={t("cockpit.trendNote", {
              mode: label(currentMode),
              n: points.length,
            })}
          >
            <div className="grid grid-3">
              <Stat
                label={t("overview.equity")}
                value={money(account?.equity_usdt)}
                note={t("risk.available", {
                  value: money(account?.available_quote_usdt),
                })}
                tone="olive"
              />
              <Stat
                label={t("overview.dailyRealized")}
                value={signed(account?.daily_realized_pnl_usdt)}
                note={pct(account?.daily_realized_pnl_pct)}
                tone={Number(account?.daily_realized_pnl_usdt ?? 0) < 0 ? "danger" : "olive"}
              />
              <Stat
                label={t("overview.drawdown")}
                value={pct(account?.drawdown_pct)}
                note={t("overview.grossNote", {
                  value: money(account?.gross_exposure_usdt),
                })}
              />
            </div>
            <div style={{ marginTop: 12 }}>
              <EquityChart points={points} height={200} />
            </div>
          </PaperCard>

          <PaperCard eyebrow={t("cockpit.positions")} title={t("cockpit.positions")}>
            {view?.positions?.length ? (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>{t("common.symbol")}</th>
                      <th className="num">{t("common.qty")}</th>
                      <th className="num">{t("common.entry")}</th>
                      <th className="num">{t("common.mark")}</th>
                      <th className="num">{t("common.unrealized")}</th>
                      <th>{t("common.opened")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {view.positions.map((position) => (
                      <tr key={position.id}>
                        <td>
                          <strong>{position.symbol}</strong>
                        </td>
                        <td className="num">{position.qty}</td>
                        <td className="num">{money(position.avg_entry_price)}</td>
                        <td className="num">{money(position.mark_price)}</td>
                        <td className="num">{signed(position.unrealized_pnl_usdt)}</td>
                        <td className="muted">{timeAgo(position.opened_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title={t("cockpit.noPositions")} />
            )}
          </PaperCard>

          <PaperCard eyebrow={t("cockpit.recentOrders")} title={t("cockpit.recentOrders")}>
            {orders.data?.items?.length ? (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>{t("orders.order")}</th>
                      <th>{t("common.symbol")}</th>
                      <th>{t("common.side")}</th>
                      <th>{t("common.status")}</th>
                      <th className="num">{t("common.qty")}</th>
                      <th className="num">{t("orders.avgPrice")}</th>
                      <th>{t("common.created")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {orders.data.items.map((order) => (
                      <tr key={order.id}>
                        <td className="mono">{shortId(order.id, 7)}</td>
                        <td>
                          <strong>{order.symbol}</strong>
                        </td>
                        <td>{label(order.side)}</td>
                        <td>
                          <Pill tone={order.status === "FILLED" ? "olive" : "yellow"}>
                            {label(order.status)}
                          </Pill>
                        </td>
                        <td className="num">{order.filled_qty || order.qty}</td>
                        <td className="num">{money(order.avg_fill_price)}</td>
                        <td className="muted">{timeAgo(order.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title={t("cockpit.noOrders")} />
            )}
          </PaperCard>
        </div>

        <div className="stack">
          <PaperCard eyebrow={t("cockpit.automation")} title={t("cockpit.automation")}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <div>
                <div className="stat-value">
                  {automation?.enabled ? t("cockpit.autoOn") : t("cockpit.autoOff")}
                </div>
                <div className="stat-note">
                  {automation?.enabled && automation.next_run_at
                    ? t("cockpit.nextRun", { time: countdown(automation.next_run_at) })
                    : t("cockpit.noNextRun")}
                </div>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={Boolean(automation?.enabled)}
                className={`switch${automation?.enabled ? " on" : ""}`}
                onClick={() => void setAutomation(!automation?.enabled)}
                disabled={busy !== null}
              >
                <span />
              </button>
            </div>
            <div className="row" style={{ marginTop: 12 }}>
              <span className="small muted">{t("cockpit.interval")}</span>
              <select
                className="input"
                style={{ width: 140 }}
                value={automation?.interval_seconds ?? 300}
                onChange={(event) =>
                  void setAutomation(
                    Boolean(automation?.enabled),
                    Number(event.target.value),
                  )
                }
                disabled={busy !== null}
              >
                {INTERVALS.map((seconds) => (
                  <option key={seconds} value={seconds}>
                    {t("cockpit.minutes", { n: seconds / 60 })}
                  </option>
                ))}
              </select>
            </div>
            <p className="small" style={{ marginTop: 12, color: "var(--ink-soft)" }}>
              {t("cockpit.autoBody")}
            </p>
            <button
              className="button small"
              style={{ marginTop: 12 }}
              onClick={() => void runNow()}
              disabled={busy !== null}
            >
              {t("cockpit.runNow")}
            </button>
          </PaperCard>

          <PaperCard eyebrow={t("cockpit.models")} title={t("cockpit.models")}>
            <div className="stack">
              {(["llm", "jev"] as const).map((key) => {
                const connection = models?.[key];
                return (
                  <div
                    key={key}
                    className="row"
                    style={{ justifyContent: "space-between", gap: 12 }}
                  >
                    <div style={{ minWidth: 0 }}>
                      <strong>{key === "llm" ? t("cockpit.llm") : t("cockpit.jev")}</strong>
                      <div className="muted small">
                        {connection?.configured
                          ? `${label(connection.provider)} · ${connection.model ?? "—"}`
                          : t("cockpit.simulatedFallback")}
                      </div>
                    </div>
                    <Pill tone={connection?.configured ? "olive" : "terracotta"} dot>
                      {connection?.configured
                        ? t("cockpit.configured")
                        : t("cockpit.notConfigured")}
                    </Pill>
                  </div>
                );
              })}
              <p className="small" style={{ color: "var(--ink-soft)" }}>
                {t("cockpit.modelsBody")}
              </p>
              <button className="button small" onClick={() => setModelsOpen(true)}>
                {t("cockpit.configure")}
              </button>
            </div>
          </PaperCard>
        </div>
      </div>

      <ModelSettingsModal
        open={modelsOpen}
        models={models}
        onClose={() => setModelsOpen(false)}
        onSaved={() => {
          void status.reload();
        }}
      />
    </div>
  );
}
