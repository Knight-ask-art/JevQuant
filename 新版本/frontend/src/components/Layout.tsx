import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { api, eventStreamUrl } from "../lib/api";
import { isHealthy, label, timeAgo } from "../lib/format";
import { useApi } from "../hooks/useApi";
import { LanguageSwitch, useI18n } from "../i18n";
import type { SystemStatus } from "../lib/types";
import { Pill } from "./ui";

const ADVANCED = [
  ["/overview", "nav.overview"],
  ["/strategy", "nav.strategy"],
  ["/signals", "nav.signals"],
  ["/orders", "nav.orders"],
  ["/backtests", "nav.backtests"],
  ["/models", "nav.models"],
  ["/risk", "nav.risk"],
  ["/audit", "nav.audit"],
] as const;

function dependencyTone(healthy: boolean): string {
  return healthy ? "ok" : "bad";
}

export function Layout() {
  const { t } = useI18n();
  const location = useLocation();
  const [advancedOpen, setAdvancedOpen] = useState(
    () =>
      window.localStorage.getItem("jev.nav.advanced") === "1" ||
      ADVANCED.some(([to]) => location.pathname === to),
  );
  useEffect(() => {
    window.localStorage.setItem("jev.nav.advanced", advancedOpen ? "1" : "0");
  }, [advancedOpen]);
  const status = useApi<SystemStatus>("/api/v1/system/status", 10000);
  const [killError, setKillError] = useState<string | null>(null);
  const [lastEvent, setLastEvent] = useState<string>("");
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let closed = false;
    let timer: number | undefined;
    const connect = () => {
      if (closed) return;
      const socket = new WebSocket(eventStreamUrl());
      socketRef.current = socket;
      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(message.data) as {
            topic?: string;
            payload?: { event?: string; mode?: string };
          };
          const name = event.payload?.event ?? event.topic ?? "";
          if (name && name !== "heartbeat") setLastEvent(name);
        } catch {
          /* ignore malformed frames */
        }
      };
      socket.onclose = () => {
        if (!closed) timer = window.setTimeout(connect, 4000);
      };
    };
    connect();
    return () => {
      closed = true;
      if (timer) window.clearTimeout(timer);
      socketRef.current?.close();
    };
  }, []);

  const view = status.data;
  // Expiry is derived server-side so a stale ACTIVE row never reads valid.
  const policyActive = view?.policy_state?.active ?? Boolean(view?.policy);
  const mode = view?.mode;
  const killSwitch = mode?.kill_switch ?? false;
  const armed = mode?.live_armed ?? false;
  const isLive = mode?.mode === "live";
  const modeTone = isLive ? "danger" : mode?.mode === "testnet" ? "yellow" : "olive";

  async function stopEntries() {
    setKillError(null);
    try {
      await api.post("/api/v1/kill-switch", { reason: "operator:ribbon" });
      await status.reload();
    } catch (error) {
      setKillError(String(error));
    }
  }

  return (
    <div className="app-shell">
      <div className="layout">
        <aside className="sidebar">
          <div className="brand">
            <span className="brand-mark">Jev Trading</span>
            <span className="brand-sub">{t("brand.sub")}</span>
          </div>

          <nav className="nav">
            <NavLink to="/" end>
              {t("nav.cockpit")}
            </NavLink>
            <NavLink to="/agent" end>
              {t("nav.agent")}
            </NavLink>
            <button
              type="button"
              className={`nav-toggle${advancedOpen ? " open" : ""}`}
              onClick={() => setAdvancedOpen((value) => !value)}
              aria-expanded={advancedOpen}
            >
              <span>{t("nav.advanced")}</span>
              <span aria-hidden="true">{advancedOpen ? "−" : "+"}</span>
            </button>
            {advancedOpen &&
              ADVANCED.map(([to, key]) => (
                <NavLink key={to} to={to} end>
                  {t(key)}
                </NavLink>
              ))}
          </nav>

          <div className="sidebar-foot">
            <div className="eyebrow">{t("sidebar.marginNotes")}</div>
            <div className="note-list">
              <span className={dependencyTone(isHealthy(view?.dependencies.market))}>
                {t("sidebar.marketFeed")} · {view?.data_source ?? "…"}
              </span>
              <span className={dependencyTone(isHealthy(view?.dependencies.database))}>
                {t("sidebar.database")}
              </span>
              <span className={dependencyTone(isHealthy(view?.dependencies.execution))}>
                {t("sidebar.execution")} · {view?.execution_backend ?? "…"}
              </span>
              <span className={dependencyTone(policyActive)}>
                {policyActive ? t("sidebar.policyValid") : t("sidebar.policyMissing")}
              </span>
              <span className={dependencyTone(isHealthy(view?.dependencies.jev))}>
                {view?.dependencies.jev?.simulated
                  ? t("sidebar.jevSimulated")
                  : t("sidebar.jevConnected")}
              </span>
            </div>
            <div className="hand" style={{ marginTop: 8 }}>
              {view?.synthetic_market
                ? t("sidebar.syntheticNote")
                : t("sidebar.liveNote")}
            </div>
          </div>
        </aside>

        <main className="main">
          <div className="mode-ribbon">
            <Pill tone={modeTone as "" | "olive" | "yellow" | "danger"} dot>
              {mode?.mode ? label(mode.mode) : t("ribbon.connecting")}
            </Pill>
            {isLive && (
              <Pill tone={armed ? "danger" : "yellow"}>
                {armed ? t("ribbon.armed") : t("ribbon.notArmed")}
              </Pill>
            )}
            {killSwitch && <Pill tone="danger">{t("ribbon.entriesStopped")}</Pill>}
            {view?.simulated_models && (
              <Pill tone="terracotta">{t("ribbon.simulatedModels")}</Pill>
            )}
            {view?.synthetic_market && <Pill>{t("ribbon.syntheticData")}</Pill>}
            <span className="spacer" />
            {lastEvent && (
              <span className="muted small">
                {t("ribbon.event")} · {label(lastEvent)}
              </span>
            )}
            <span className="muted small">
              {t("ribbon.updated", {
                time: timeAgo(view?.server_time ?? null),
                status: view?.server_time ? "" : ` · ${t("ribbon.offline")}`,
              })}
            </span>
            <button
              className="button danger small"
              onClick={() => void stopEntries()}
              disabled={killSwitch}
            >
              {t("ribbon.stopEntries")}
            </button>
            <LanguageSwitch />
          </div>

          {killError && <div className="callout danger">{killError}</div>}
          <Outlet context={{ status: view, reloadStatus: status.reload }} />
        </main>
      </div>
    </div>
  );
}
