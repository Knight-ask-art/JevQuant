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
import { useI18n } from "../i18n";
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
  const { t } = useI18n();
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
          <div className="page-kicker">
            {t("overview.kicker", { time: clock(view?.server_time) })}
          </div>
          <h1>{t("overview.title")}</h1>
          <ScribbleUnderline width={340} />
          <p className="page-lede">{t("overview.lede")}</p>
        </div>
        <div className="row">
          <Pill tone={view?.synthetic_market ? "terracotta" : "olive"}>
            {view?.synthetic_market
              ? t("overview.syntheticMarket")
              : t("overview.liveMarket")}
          </Pill>
          <button className="button" onClick={() => void runCycle()} disabled={busy}>
            {busy ? t("common.running") : t("common.runCycle")}
          </button>
        </div>
      </div>

      {error && <ErrorNote error={error} />}
      {cycle && (
        <div className="callout olive">
          {t("overview.cycleSummary", {
            id: shortId(cycle.cycle_id),
            created: cycle.signals_created,
            eligible: cycle.signals_eligible,
            approved: cycle.approved,
            rejected: cycle.rejected,
            executed: cycle.executed,
            exits: cycle.exits,
          })}
          {cycle.entries_blocked
            ? t("overview.guardBlocked", { n: cycle.entries_blocked })
            : ""}
          {cycle.entries_blocked_reason
            ? t("overview.entriesBlocked", {
                reason: label(cycle.entries_blocked_reason),
              })
            : ""}
        </div>
      )}

      <div className="grid grid-4">
        <PaperCard>
          <Stat
            label={t("overview.equity")}
            value={money(account?.equity_usdt)}
            note={t("overview.equityNote", {
              mode: label(view?.mode.mode ?? "paper"),
            })}
            tone="olive"
          />
        </PaperCard>
        <PaperCard>
          <Stat
            label={t("overview.availableQuote")}
            value={money(account?.available_quote_usdt)}
            note={t("overview.grossNote", {
              value: money(account?.gross_exposure_usdt),
            })}
          />
        </PaperCard>
        <PaperCard>
          <Stat
            label={t("overview.dailyRealized")}
            value={signed(account?.daily_realized_pnl_usdt)}
            note={pct(account?.daily_realized_pnl_pct)}
            tone={Number(account?.daily_realized_pnl_usdt ?? 0) < 0 ? "danger" : "olive"}
          />
        </PaperCard>
        <PaperCard>
          <Stat
            label={t("overview.drawdown")}
            value={pct(account?.drawdown_pct)}
            note={t("overview.drawdownCap", {
              value: pct(limits?.max_drawdown_pct, 1),
            })}
            tone={Number(account?.drawdown_pct ?? 0) > 3 ? "danger" : undefined}
          />
        </PaperCard>
      </div>

      <div className="two-col">
        <div className="stack">
          <PaperCard
            eyebrow={t("overview.jevOpportunities")}
            title={t("overview.typedSignals")}
            actions={
              <Link className="small" to="/signals">
                {t("overview.openSignalDesk")}
              </Link>
            }
          >
            {signals.loading && !signals.data ? (
              <div className="loading">{t("common.fetchingSignals")}</div>
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
                          {label(choice)} · {timeAgo(signal.created_at)} ·{" "}
                          {shortId(signal.id)}
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
                title={t("overview.noSignals")}
                hint={t("overview.noSignalsHint")}
                action={
                  <button
                    className="button small"
                    onClick={() => void runCycle()}
                    disabled={busy}
                  >
                    {t("common.runCycle")}
                  </button>
                }
              />
            )}
          </PaperCard>

          <PaperCard eyebrow={t("overview.positions")} title={t("overview.openExposure")}>
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
              <EmptyState title={t("overview.flat")} hint={t("overview.flatHint")} />
            )}
          </PaperCard>
        </div>

        <div className="stack">
          <PaperCard
            eyebrow={t("overview.activeStrategy")}
            title={
              policy
                ? label(
                    String(
                      (policy.raw as { regime?: { label?: string } })?.regime?.label ??
                        "policy",
                    ),
                  )
                : t("overview.noActivePolicy")
            }
          >
            {policy ? (
              <div className="stack">
                <div className="row">
                  <Pill tone={policy.simulated ? "terracotta" : "olive"}>
                    {policy.simulated ? t("overview.simulatedAuthor") : policy.source}
                  </Pill>
                  <Pill tone="yellow">
                    {t("overview.expiresIn", { time: countdown(policy.expires_at) })}
                  </Pill>
                </div>
                <p className="small" style={{ color: "var(--ink-soft)" }}>
                  {String((policy.raw as { rationale?: string })?.rationale ?? "")}
                </p>
                <div className="kv">
                  <dt>{t("overview.policyHash")}</dt>
                  <dd className="mono">{shortId(policy.policy_hash, 18)}</dd>
                  <dt>{t("overview.clampsApplied")}</dt>
                  <dd>{policy.clamps.length}</dd>
                  <dt>{t("overview.scope")}</dt>
                  <dd>{policy.mode_scope?.join(", ")}</dd>
                </div>
                <Link className="small" to="/strategy">
                  {t("overview.inspectNotebook")}
                </Link>
              </div>
            ) : (
              <div className="stack">
                {view?.policy_state?.expired && (
                  <p className="small" style={{ color: "var(--terracotta)" }}>
                    {t("overview.policyExpired", {
                      time: clock(view.policy_state.expires_at),
                    })}
                  </p>
                )}
                <p className="hand">{t("overview.noPolicy")}</p>
                <Link className="small" to="/strategy">
                  {t("overview.generateOne")}
                </Link>
              </div>
            )}
          </PaperCard>

          <PaperCard eyebrow={t("overview.riskEnvelope")} title={t("overview.hardLimits")}>
            {limits ? (
              <div className="stack">
                <div>
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <span className="small">{t("overview.grossExposure")}</span>
                    <span className="small mono">
                      {money(account?.gross_exposure_usdt)} /{" "}
                      {t("overview.ofEquity", {
                        value: pct(limits.max_gross_exposure_pct, 0),
                      })}
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
                    <span className="small">{t("overview.openPositions")}</span>
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
                  <dt>{t("overview.maxPosition")}</dt>
                  <dd className="mono">{limits.max_position_notional_usdt} USDT</dd>
                  <dt>{t("overview.dailyLossCap")}</dt>
                  <dd className="mono">{limits.max_daily_loss_pct}%</dd>
                  <dt>{t("overview.universe")}</dt>
                  <dd className="mono">{limits.allowed_symbols.join(" · ")}</dd>
                </div>
              </div>
            ) : (
              <div className="loading">{t("common.fetchingLimits")}</div>
            )}
          </PaperCard>

          <PaperCard eyebrow={t("overview.peerReview")} title={t("overview.dependencyNotes")}>
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

      <PaperCard eyebrow={t("overview.ledger")} title={t("overview.equitySnapshots")}>
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
