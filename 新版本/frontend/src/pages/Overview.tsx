import { useState } from "react";
import { Link, useOutletContext } from "react-router-dom";

import { EquityChart, ProbabilitySketch } from "../components/charts";
import {
  EmptyState,
  ErrorNote,
  Meter,
  PaperCard,
  Pill,
  ScribbleUnderline,
  Stat,
} from "../components/ui";
import { useApi } from "../hooks/useApi";
import { ApiError, api } from "../lib/api";
import {
  clock,
  countdown,
  label,
  money,
  pct,
  shortId,
  isHealthy,
  signed,
  timeAgo,
  toneForState,
} from "../lib/format";
import type { CycleResult, PnlPoint, SignalView, SystemStatus } from "../lib/types";

export function Overview() {
  const status = useApi<SystemStatus>("/api/v1/system/status", 8000);
  const signals = useApi<{ items: SignalView[] }>("/api/v1/signals?limit=6", 8000);
  const pnl = useApi<{ items: PnlPoint[] }>("/api/v1/pnl?limit=120", 15000);
  const [cycle, setCycle] = useState<CycleResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const { reloadStatus } = useOutletContext<{ reloadStatus: () => Promise<void> }>();

  const view = status.data;
  const account = view?.account;
  const limits = view?.server_limits;
  const policy = view?.policy;

  async function runCycle() {
    setBusy(true);
    setError(null);
    try {
      setCycle(await api.post<CycleResult>("/api/v1/demo/cycle"));
      await Promise.all([status.reload(), signals.reload(), pnl.reload(), reloadStatus()]);
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setBusy(false);
    }
  }

  if (status.error) {
    return (
      <div className="stack">
        <ErrorNote error={status.error} />
        <div className="callout">
          Start the control plane with <span className="mono">jev serve</span> and refresh.
        </div>
      </div>
    );
  }

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="page-head">
        <div>
          <div className="page-kicker">Overview · {clock(view?.server_time)}</div>
          <h1>Today, the system thinks…</h1>
          <ScribbleUnderline width={340} />
          <p className="page-lede">
            An LLM proposes a bounded strategy; a deterministic compiler narrows it; Jev
            evaluates typed opportunities; an independent risk engine decides. Nothing here is
            a performance claim when data is synthetic.
          </p>
        </div>
        <div className="row">
          <Pill tone={view?.synthetic_market ? "terracotta" : "olive"}>
            {view?.synthetic_market ? "synthetic market" : "live market data"}
          </Pill>
          <button className="button" onClick={() => void runCycle()} disabled={busy}>
            {busy ? "Running…" : "Run one cycle"}
          </button>
        </div>
      </div>

      {error && <ErrorNote error={error} />}
      {cycle && (
        <div className="callout olive">
          Cycle {shortId(cycle.cycle_id)} finished: {cycle.signals_created} signals,{" "}
          {cycle.signals_eligible} eligible, {cycle.approved} approved, {cycle.rejected}{" "}
          rejected, {cycle.executed} executed, {cycle.exits} exits
          {cycle.entries_blocked_reason
            ? ` · entries blocked: ${label(cycle.entries_blocked_reason)}`
            : ""}
          .
        </div>
      )}

      <div className="grid grid-4">
        <PaperCard>
          <Stat
            label="Equity"
            value={money(account?.equity_usdt)}
            note="USDT · paper ledger"
            tone="olive"
          />
        </PaperCard>
        <PaperCard>
          <Stat
            label="Available quote"
            value={money(account?.available_quote_usdt)}
            note={`gross ${money(account?.gross_exposure_usdt)}`}
          />
        </PaperCard>
        <PaperCard>
          <Stat
            label="Daily realized"
            value={signed(account?.daily_realized_pnl_usdt)}
            note={pct(account?.daily_realized_pnl_pct)}
            tone={Number(account?.daily_realized_pnl_usdt ?? 0) < 0 ? "danger" : "olive"}
          />
        </PaperCard>
        <PaperCard>
          <Stat
            label="Drawdown"
            value={pct(account?.drawdown_pct)}
            note={`cap ${pct(limits?.max_drawdown_pct, 1)}`}
            tone={Number(account?.drawdown_pct ?? 0) > 3 ? "danger" : undefined}
          />
        </PaperCard>
      </div>

      <div className="two-col">
        <div className="stack">
          <PaperCard
            eyebrow="Jev opportunities"
            title="Typed signals, newest first"
            actions={
              <Link className="small" to="/signals">
                Open signal desk →
              </Link>
            }
          >
            {signals.loading && !signals.data ? (
              <div className="loading">Fetching signals…</div>
            ) : signals.data?.items?.length ? (
              <div className="stack">
                {signals.data.items.map((signal) => {
                  const probability = Number(signal.score?.entry_probability ?? 0.5);
                  const fit = Number(signal.score?.policy_fit ?? 0.5);
                  const choice = String(signal.score?.entry_choice ?? "HOLD");
                  return (
                    <div
                      key={signal.id}
                      className="row"
                      style={{
                        justifyContent: "space-between",
                        borderBottom: "1px dashed var(--line)",
                        paddingBottom: 10,
                      }}
                    >
                      <div style={{ minWidth: 220 }}>
                        <div className="row" style={{ gap: 8 }}>
                          <strong>{signal.symbol}</strong>
                          <Pill
                            tone={
                              toneForState(signal.state) as
                                | ""
                                | "olive"
                                | "terracotta"
                                | "danger"
                                | "yellow"
                            }
                          >
                            {label(signal.state)}
                          </Pill>
                        </div>
                        <div className="muted small">
                          {choice} · {timeAgo(signal.created_at)} · {shortId(signal.id)}
                        </div>
                      </div>
                      <ProbabilitySketch
                        buy={choice === "BUY" ? probability : Math.min(0.45, probability)}
                        sell={choice === "SELL" ? probability : 0.08}
                        hold={0.4}
                        fit={fit}
                      />
                    </div>
                  );
                })}
              </div>
            ) : (
              <EmptyState
                title="No signals written yet."
                hint="Run a cycle to let Jev evaluate the allowed universe."
                action={
                  <button className="button small" onClick={() => void runCycle()} disabled={busy}>
                    Run one cycle
                  </button>
                }
              />
            )}
          </PaperCard>

          <PaperCard eyebrow="Positions" title="Open exposure">
            {view?.positions?.length ? (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Symbol</th>
                      <th className="num">Qty</th>
                      <th className="num">Entry</th>
                      <th className="num">Mark</th>
                      <th className="num">Unrealized</th>
                      <th>Opened</th>
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
              <EmptyState title="Flat. No open positions." hint="Risk never pyramids on spot." />
            )}
          </PaperCard>
        </div>

        <div className="stack">
          <PaperCard
            eyebrow="Active strategy"
            title={
              policy
                ? label(
                    String(
                      (policy.raw as { regime?: { label?: string } })?.regime?.label ??
                        "policy",
                    ),
                  )
                : "No active policy"
            }
          >
            {policy ? (
              <div className="stack">
                <div className="row">
                  <Pill tone={policy.simulated ? "terracotta" : "olive"}>
                    {policy.simulated ? "simulated author" : policy.source}
                  </Pill>
                  <Pill tone="yellow">expires in {countdown(policy.expires_at)}</Pill>
                </div>
                <p className="small" style={{ color: "var(--ink-soft)" }}>
                  {String((policy.raw as { rationale?: string })?.rationale ?? "")}
                </p>
                <div className="kv">
                  <dt>Policy hash</dt>
                  <dd className="mono">{shortId(policy.policy_hash, 18)}</dd>
                  <dt>Clamps applied</dt>
                  <dd>{policy.clamps.length}</dd>
                  <dt>Scope</dt>
                  <dd>{policy.mode_scope?.join(", ")}</dd>
                </div>
                <Link className="small" to="/strategy">
                  Inspect the notebook →
                </Link>
              </div>
            ) : (
              <div className="stack">
                <p className="hand">No policy on the desk yet.</p>
                <Link className="small" to="/strategy">
                  Generate one →
                </Link>
              </div>
            )}
          </PaperCard>

          <PaperCard eyebrow="Risk envelope" title="Hard server limits">
            {limits ? (
              <div className="stack">
                <div>
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <span className="small">Gross exposure</span>
                    <span className="small mono">
                      {money(account?.gross_exposure_usdt)} /{" "}
                      {pct(limits.max_gross_exposure_pct, 0)} of equity
                    </span>
                  </div>
                  <Meter
                    value={Number(account?.gross_exposure_usdt ?? 0)}
                    max={
                      (Number(account?.equity_usdt ?? 0) *
                        Number(limits.max_gross_exposure_pct)) /
                      100
                    }
                  />
                </div>
                <div>
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <span className="small">Open positions</span>
                    <span className="small mono">
                      {account?.open_positions ?? 0} / {limits.max_positions}
                    </span>
                  </div>
                  <Meter
                    value={account?.open_positions ?? 0}
                    max={limits.max_positions}
                    tone={
                      (account?.open_positions ?? 0) >= limits.max_positions
                        ? "warn"
                        : undefined
                    }
                  />
                </div>
                <div className="kv">
                  <dt>Max position</dt>
                  <dd className="mono">{limits.max_position_notional_usdt} USDT</dd>
                  <dt>Daily loss cap</dt>
                  <dd className="mono">{limits.max_daily_loss_pct}%</dd>
                  <dt>Universe</dt>
                  <dd className="mono">{limits.allowed_symbols.join(" · ")}</dd>
                </div>
              </div>
            ) : (
              <div className="loading">Fetching limits…</div>
            )}
          </PaperCard>

          <PaperCard eyebrow="Peer review" title="Dependency margin notes">
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
          </PaperCard>
        </div>
      </div>

      <PaperCard eyebrow="Ledger" title="Equity snapshots">
        <EquityChart
          points={(pnl.data?.items ?? [])
            .slice()
            .reverse()
            .map((point) => ({ at: point.as_of, equity_usdt: point.equity_usdt }))}
        />
      </PaperCard>
    </div>
  );
}
