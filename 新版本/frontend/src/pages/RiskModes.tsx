import { useState } from "react";
import { useOutletContext } from "react-router-dom";

import { LiveArmingModal } from "../components/LiveArmingModal";
import {
  ErrorNote,
  Field,
  Meter,
  PaperCard,
  Pill,
  ScribbleUnderline,
  Stat,
} from "../components/ui";
import { useApi } from "../hooks/useApi";
import { useI18n } from "../i18n";
import { ApiError, api } from "../lib/api";
import { clock, isHealthy, label, money, pct, timeAgo } from "../lib/format";
import type { SystemStatus } from "../lib/types";

export function RiskModes() {
  const { t } = useI18n();
  const status = useApi<SystemStatus>("/api/v1/system/status", 6000);
  const [armingOpen, setArmingOpen] = useState(false);
  const [reason, setReason] = useState("operator:disarm");
  const [error, setError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const { reloadStatus } = useOutletContext<{ reloadStatus: () => Promise<void> }>();

  const view = status.data;
  const mode = view?.mode;
  const account = view?.account;
  const limits = view?.server_limits;

  async function action(
    name: string,
    path: string,
    body: unknown | undefined,
    success: string,
  ) {
    setBusy(name);
    setError(null);
    setNotice(null);
    try {
      if (body === undefined) await api.post(path);
      else await api.post(path, body);
      setNotice(success);
      await Promise.all([status.reload(), reloadStatus()]);
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="page-head">
        <div>
          <div className="page-kicker">{t("risk.kicker")}</div>
          <h1>{t("risk.title")}</h1>
          <ScribbleUnderline width={380} />
          <p className="page-lede">{t("risk.lede")}</p>
        </div>
        <div className="row">
          <Pill tone={mode?.mode === "live" ? "danger" : "olive"} dot>
            {mode?.mode ? label(mode.mode) : "—"}
          </Pill>
          {mode?.live_armed && <Pill tone="danger">{t("risk.armed")}</Pill>}
          {mode?.kill_switch && <Pill tone="danger">{t("risk.entriesStopped")}</Pill>}
        </div>
      </div>

      {error && <ErrorNote error={error} />}
      {notice && <div className="callout olive">{notice}</div>}

      <div className="grid grid-3">
        <PaperCard eyebrow={t("risk.mode")} title={t("risk.executionRoute")}>
          <div className="stack">
            <div className="segmented">
              {(["paper", "shadow", "testnet"] as const).map((value) => (
                <button
                  key={value}
                  className={mode?.mode === value ? "active" : ""}
                  onClick={() =>
                    void action(
                      value,
                      "/api/v1/mode/set",
                      { mode: value },
                      t("risk.modeSet", { mode: label(value) }),
                    )
                  }
                  disabled={busy !== null || mode?.mode === value}
                >
                  {label(value)}
                </button>
              ))}
            </div>
            <div className="kv">
              <dt>{t("risk.backend")}</dt>
              <dd className="mono">{label(mode?.execution_backend)}</dd>
              <dt>{t("risk.armingStatus")}</dt>
              <dd className="mono">{label(mode?.arming_status)}</dd>
              <dt>{t("risk.armedAt")}</dt>
              <dd className="mono">{clock(mode?.armed_at)}</dd>
              <dt>{t("risk.expires")}</dt>
              <dd className="mono">{clock(mode?.arming_expires_at)}</dd>
              <dt>{t("risk.lastReason")}</dt>
              <dd className="mono small">{mode?.last_reason ?? "—"}</dd>
            </div>
            <div className="row">
              <button
                className="button danger small"
                onClick={() => setArmingOpen(true)}
                disabled={mode?.mode === "live" && Boolean(mode?.live_armed)}
              >
                {t("risk.preflightArm")}
              </button>
              {mode?.live_armed && (
                <button
                  className="button ghost small"
                  onClick={() =>
                    void action(
                      "disarm",
                      "/api/v1/mode/disarm",
                      { reason },
                      t("risk.disarmed"),
                    )
                  }
                  disabled={busy !== null}
                >
                  {t("risk.disarmLive")}
                </button>
              )}
            </div>
            <Field label={t("risk.disarmReason")} hint={t("risk.disarmReasonHint")}>
              <input
                className="input"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </Field>
          </div>
        </PaperCard>

        <PaperCard eyebrow={t("risk.killSwitch")} title={t("risk.stopNewEntries")}>
          <p className="small" style={{ color: "var(--ink-soft)", marginBottom: 12 }}>
            {t("risk.killBody")}
          </p>
          {mode?.kill_switch ? (
            <button
              className="button small"
              onClick={() =>
                void action(
                  "reset-kill",
                  "/api/v1/kill-switch/reset",
                  undefined,
                  t("risk.killReset"),
                )
              }
              disabled={busy !== null}
            >
              {t("risk.resetKill")}
            </button>
          ) : (
            <button
              className="button danger small"
              onClick={() =>
                void action(
                  "kill",
                  "/api/v1/kill-switch",
                  { reason: "operator:risk-page" },
                  t("risk.killActive"),
                )
              }
              disabled={busy !== null}
            >
              {t("risk.stopNewEntries")}
            </button>
          )}
          <hr className="divider" />
          <div className="note-list">
            <span className={mode?.kill_switch ? "bad" : "ok"}>
              {mode?.kill_switch ? t("risk.entriesBlocked") : t("risk.entriesPermitted")}
            </span>
            <span className="ok">{t("risk.exitsDeterministic")}</span>
            <span className="ok">{t("risk.armingRefused")}</span>
          </div>
        </PaperCard>

        <PaperCard eyebrow={t("risk.account")} title={t("risk.riskBudget")}>
          <Stat
            label={t("risk.equity")}
            value={money(account?.equity_usdt)}
            note={t("risk.available", {
              value: money(account?.available_quote_usdt),
            })}
            tone="olive"
          />
          <hr className="divider" />
          <div className="stack">
            <div>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="small">{t("risk.grossExposure")}</span>
                <span className="mono small">
                  {money(account?.gross_exposure_usdt)} / {pct(limits?.max_gross_exposure_pct, 0)}
                </span>
              </div>
              <Meter
                value={Number(account?.gross_exposure_usdt ?? 0)}
                max={
                  (Number(account?.equity_usdt ?? 0) *
                    Number(limits?.max_gross_exposure_pct ?? 0)) /
                  100
                }
              />
            </div>
            <div>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="small">{t("risk.dailyLoss")}</span>
                <span className="mono small">
                  {pct(account?.daily_realized_pnl_pct)} / -{pct(limits?.max_daily_loss_pct, 1)}
                </span>
              </div>
              <Meter
                value={Math.max(0, -Number(account?.daily_realized_pnl_pct ?? 0))}
                max={Number(limits?.max_daily_loss_pct ?? 1)}
                tone="warn"
              />
            </div>
            <div>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="small">{t("risk.drawdown")}</span>
                <span className="mono small">
                  {pct(account?.drawdown_pct)} / {pct(limits?.max_drawdown_pct, 1)}
                </span>
              </div>
              <Meter
                value={Number(account?.drawdown_pct ?? 0)}
                max={Number(limits?.max_drawdown_pct ?? 5)}
                tone="danger"
              />
            </div>
          </div>
        </PaperCard>
      </div>

      <div className="two-col">
        <PaperCard eyebrow={t("risk.hardEnvelope")} title={t("risk.serverLimits")}>
          {limits ? (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>{t("risk.limit")}</th>
                    <th>{t("risk.value")}</th>
                    <th>{t("risk.enforcedBy")}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>{t("risk.maxPositionNotional")}</td>
                    <td className="mono">{limits.max_position_notional_usdt} USDT</td>
                    <td className="muted">{t("risk.compilerRisk")}</td>
                  </tr>
                  <tr>
                    <td>{t("risk.maxGrossExposure")}</td>
                    <td className="mono">{limits.max_gross_exposure_pct}%</td>
                    <td className="muted">{t("risk.compilerRisk")}</td>
                  </tr>
                  <tr>
                    <td>{t("risk.maxDailyLoss")}</td>
                    <td className="mono">{limits.max_daily_loss_pct}%</td>
                    <td className="muted">{t("risk.risk")}</td>
                  </tr>
                  <tr>
                    <td>{t("risk.maxDrawdown")}</td>
                    <td className="mono">{limits.max_drawdown_pct}%</td>
                    <td className="muted">{t("risk.risk")}</td>
                  </tr>
                  <tr>
                    <td>{t("risk.maxOpenPositions")}</td>
                    <td className="mono">{limits.max_positions}</td>
                    <td className="muted">{t("risk.risk")}</td>
                  </tr>
                  <tr>
                    <td>{t("risk.allowedSymbols")}</td>
                    <td className="mono">{limits.allowed_symbols.join(", ")}</td>
                    <td className="muted">{t("risk.compiler")}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          ) : (
            <div className="loading">{t("common.fetchingLimits")}</div>
          )}
        </PaperCard>

        <PaperCard eyebrow={t("risk.dependencies")} title={t("risk.preflightReads")}>
          <div className="note-list">
            {Object.entries(view?.dependencies ?? {}).map(([name, health]) => (
              <span key={name} className={isHealthy(health) ? "ok" : "bad"}>
                {label(name)}
                {typeof (health as { detail?: unknown })?.detail === "string" &&
                (health as { detail?: string }).detail
                  ? ` · ${(health as { detail?: string }).detail}`
                  : ""}
              </span>
            ))}
          </div>
          <hr className="divider" />
          <div className="callout">{t("risk.failClosed")}</div>
          <p className="hand" style={{ marginTop: 10 }}>
            {t("risk.updated", {
              time: timeAgo(view?.server_time),
              mode: mode?.mode ? label(mode.mode) : "—",
            })}
          </p>
        </PaperCard>
      </div>

      <LiveArmingModal
        open={armingOpen}
        onClose={() => setArmingOpen(false)}
        onArmed={() => {
          void status.reload();
          void reloadStatus();
        }}
      />
    </div>
  );
}
