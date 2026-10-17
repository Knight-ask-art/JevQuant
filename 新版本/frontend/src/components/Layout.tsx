import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";

import { api, eventStreamUrl } from "../lib/api";
import { isHealthy, label, timeAgo } from "../lib/format";
import { useApi } from "../hooks/useApi";
import type { SystemStatus } from "../lib/types";
import { Pill } from "./ui";

const NAV = [
  ["/", "Overview"],
  ["/strategy", "Strategy Desk"],
  ["/signals", "Jev Signals"],
  ["/orders", "Orders & Positions"],
  ["/backtests", "Backtests"],
  ["/models", "Models & Costs"],
  ["/risk", "Risk & Modes"],
  ["/audit", "Audit & Settings"],
] as const;

function dependencyTone(healthy: boolean): string {
  return healthy ? "ok" : "bad";
}

export function Layout() {
  const status = useApi<SystemStatus>("/api/v1/system/status", 8000);
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
            <span className="brand-sub">strategy notebook · paper first</span>
          </div>

          <nav className="nav">
            {NAV.map(([to, text]) => (
              <NavLink key={to} to={to} end={to === "/"}>
                {text}
              </NavLink>
            ))}
          </nav>

          <div className="sidebar-foot">
            <div className="eyebrow">Margin notes</div>
            <div className="note-list">
              <span className={dependencyTone(isHealthy(view?.dependencies.market))}>
                market feed · {view?.data_source ?? "…"}
              </span>
              <span className={dependencyTone(isHealthy(view?.dependencies.database))}>
                database
              </span>
              <span className={dependencyTone(isHealthy(view?.dependencies.execution))}>
                execution · {view?.execution_backend ?? "…"}
              </span>
              <span className={dependencyTone(Boolean(view?.policy))}>
                policy {view?.policy ? "valid" : "missing"}
              </span>
              <span className={dependencyTone(isHealthy(view?.dependencies.jev))}>
                Jev · {view?.dependencies.jev?.simulated ? "simulated" : "connected"}
              </span>
            </div>
            <div className="hand" style={{ marginTop: 8 }}>
              {view?.synthetic_market
                ? "Synthetic market. Plumbing only."
                : "Live market data source."}
            </div>
          </div>
        </aside>

        <main className="main">
          <div className="mode-ribbon">
            <Pill tone={modeTone as "" | "olive" | "yellow" | "danger"} dot>
              {mode?.mode ?? "connecting"}
            </Pill>
            {isLive && <Pill tone={armed ? "danger" : "yellow"}>{armed ? "armed" : "not armed"}</Pill>}
            {killSwitch && <Pill tone="danger">entries stopped</Pill>}
            {view?.simulated_models && <Pill tone="terracotta">simulated models</Pill>}
            {view?.synthetic_market && <Pill>synthetic data</Pill>}
            <span className="spacer" />
            {lastEvent && <span className="muted small">event · {label(lastEvent)}</span>}
            <span className="muted small">
              updated {timeAgo(view?.server_time ?? null)} · {view?.server_time ? "" : "offline"}
            </span>
            <button
              className="button danger small"
              onClick={() => void stopEntries()}
              disabled={killSwitch}
            >
              Stop new entries
            </button>
          </div>

          {killError && <div className="callout danger">{killError}</div>}
          <Outlet context={{ status: view, reloadStatus: status.reload }} />
        </main>
      </div>
    </div>
  );
}
