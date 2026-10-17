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
import { ApiError, api } from "../lib/api";
import { clock, isHealthy, label, money, pct, timeAgo } from "../lib/format";
import type { SystemStatus } from "../lib/types";

export function RiskModes() {
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
          <div className="page-kicker">Risk &amp; Modes</div>
          <h1>Live is a handshake, not a toggle</h1>
          <ScribbleUnderline width={380} />
          <p className="page-lede">
            Paper, Shadow and Testnet are selectable. Live requires environment flags, a
            passing server preflight, Binance key restrictions, Hummingbot/feed/DB/Redis
            health, a typed confirmation and TOTP. The server arms atomically.
          </p>
        </div>
        <div className="row">
          <Pill tone={mode?.mode === "live" ? "danger" : "olive"} dot>
            {mode?.mode ?? "—"}
          </Pill>
          {mode?.live_armed && <Pill tone="danger">armed</Pill>}
          {mode?.kill_switch && <Pill tone="danger">entries stopped</Pill>}
        </div>
      </div>

      {error && <ErrorNote error={error} />}
      {notice && <div className="callout olive">{notice}</div>}

      <div className="grid grid-3">
        <PaperCard eyebrow="Mode" title="Execution route">
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
                      `Mode set to ${value}.`,
                    )
                  }
                  disabled={busy !== null || mode?.mode === value}
                >
                  {label(value)}
                </button>
              ))}
            </div>
            <div className="kv">
              <dt>Backend</dt>
              <dd className="mono">{mode?.execution_backend}</dd>
              <dt>Arming status</dt>
              <dd className="mono">{mode?.arming_status}</dd>
              <dt>Armed at</dt>
              <dd className="mono">{clock(mode?.armed_at)}</dd>
              <dt>Expires</dt>
              <dd className="mono">{clock(mode?.arming_expires_at)}</dd>
              <dt>Last reason</dt>
              <dd className="mono small">{mode?.last_reason ?? "—"}</dd>
            </div>
            <div className="row">
              <button
                className="button danger small"
                onClick={() => setArmingOpen(true)}
                disabled={mode?.mode === "live" && Boolean(mode?.live_armed)}
              >
                Preflight &amp; arm live
              </button>
              {mode?.live_armed && (
                <button
                  className="button ghost small"
                  onClick={() =>
                    void action(
                      "disarm",
                      "/api/v1/mode/disarm",
                      { reason },
                      "Live disarmed; existing positions keep their exits.",
                    )
                  }
                  disabled={busy !== null}
                >
                  Disarm live
                </button>
              )}
            </div>
            <Field label="Disarm reason" hint="Recorded in the audit stream">
              <input
                className="input"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </Field>
          </div>
        </PaperCard>

        <PaperCard eyebrow="Kill switch" title="Stop new entries">
          <p className="small" style={{ color: "var(--ink-soft)", marginBottom: 12 }}>
            The kill switch blocks every new entry immediately. It deliberately leaves the
            mode in place so existing positions continue through their deterministic exits.
          </p>
          {mode?.kill_switch ? (
            <button
              className="button small"
              onClick={() =>
                void action(
                  "reset-kill",
                  "/api/v1/kill-switch/reset",
                  undefined,
                  "Kill switch reset. New entries are possible again.",
                )
              }
              disabled={busy !== null}
            >
              Reset kill switch
            </button>
          ) : (
            <button
              className="button danger small"
              onClick={() =>
                void action(
                  "kill",
                  "/api/v1/kill-switch",
                  { reason: "operator:risk-page" },
                  "Kill switch active. New entries are blocked.",
                )
              }
              disabled={busy !== null}
            >
              Stop new entries
            </button>
          )}
          <hr className="divider" />
          <div className="note-list">
            <span className={mode?.kill_switch ? "bad" : "ok"}>
              {mode?.kill_switch ? "Entries blocked" : "Entries permitted when other gates pass"}
            </span>
            <span className="ok">Exits remain deterministic even while halted</span>
            <span className="ok">Live arming is refused while the kill switch is active</span>
          </div>
        </PaperCard>

        <PaperCard eyebrow="Account" title="Risk budget">
          <Stat
            label="Equity"
            value={money(account?.equity_usdt)}
            note={`available ${money(account?.available_quote_usdt)}`}
            tone="olive"
          />
          <hr className="divider" />
          <div className="stack">
            <div>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="small">Gross exposure</span>
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
                <span className="small">Daily loss</span>
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
                <span className="small">Drawdown</span>
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
        <PaperCard eyebrow="Hard envelope" title="Server limits">
          {limits ? (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Limit</th>
                    <th>Value</th>
                    <th>Enforced by</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>Max position notional</td>
                    <td className="mono">{limits.max_position_notional_usdt} USDT</td>
                    <td className="muted">compiler + risk</td>
                  </tr>
                  <tr>
                    <td>Max gross exposure</td>
                    <td className="mono">{limits.max_gross_exposure_pct}%</td>
                    <td className="muted">compiler + risk</td>
                  </tr>
                  <tr>
                    <td>Max daily realized loss</td>
                    <td className="mono">{limits.max_daily_loss_pct}%</td>
                    <td className="muted">risk</td>
                  </tr>
                  <tr>
                    <td>Max drawdown</td>
                    <td className="mono">{limits.max_drawdown_pct}%</td>
                    <td className="muted">risk</td>
                  </tr>
                  <tr>
                    <td>Max open positions</td>
                    <td className="mono">{limits.max_positions}</td>
                    <td className="muted">risk</td>
                  </tr>
                  <tr>
                    <td>Allowed symbols</td>
                    <td className="mono">{limits.allowed_symbols.join(", ")}</td>
                    <td className="muted">compiler</td>
                  </tr>
                </tbody>
              </table>
            </div>
          ) : (
            <div className="loading">Fetching limits…</div>
          )}
        </PaperCard>

        <PaperCard eyebrow="Dependencies" title="Live preflight reads these">
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
          <div className="callout">
            If any precondition fails, arming is refused. The system fails closed; it does not
            degrade into trading.
          </div>
          <p className="hand" style={{ marginTop: 10 }}>
            updated {timeAgo(view?.server_time)} · mode {mode?.mode}
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
