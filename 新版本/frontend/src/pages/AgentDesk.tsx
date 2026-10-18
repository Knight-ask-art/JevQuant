import { useState } from "react";
import { Link } from "react-router-dom";

import {
  EmptyState,
  ErrorNote,
  PaperCard,
  Pill,
  ScribbleUnderline,
} from "../components/ui";
import { useApi } from "../hooks/useApi";
import { useI18n, type Key } from "../i18n";
import { ApiError, api } from "../lib/api";
import { clock, label, pct, shortId, signed, timeAgo } from "../lib/format";
import type {
  AgentRun,
  AgentRunDetail,
  AgentToolCall,
  SystemStatus,
} from "../lib/types";

interface RawPolicy {
  rationale?: string;
  regime?: { label?: string; confidence?: number; notes?: string };
  invalidation?: string[];
  jev_gate?: Record<string, unknown>;
  exit_defaults?: Record<string, unknown>;
}

interface EffectivePolicy {
  universe?: Array<{ symbol: string; max_position_notional_usdt: string }>;
  regime?: { label?: string; confidence?: number; notes?: string };
}

const TOOL_LABELS: Record<string, Key> = {
  get_market_overview: "agent.tool.market",
  get_candles: "agent.tool.candles",
  get_account_state: "agent.tool.account",
  get_open_positions: "agent.tool.positions",
  get_recent_decisions: "agent.tool.decisions",
  get_recent_trades: "agent.tool.trades",
  get_performance_summary: "agent.tool.performance",
};

export function AgentDesk() {
  const { t } = useI18n();
  const status = useApi<SystemStatus>("/api/v1/system/status", 5000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [openRun, setOpenRun] = useState<string | null>(null);
  const [traces, setTraces] = useState<Record<string, AgentRunDetail>>({});
  const [traceError, setTraceError] = useState<string | null>(null);

  const view = status.data;
  const agent = view?.agent;
  const policy = view?.policy;
  const raw = policy?.raw as RawPolicy | undefined;
  const effective = policy?.effective as EffectivePolicy | undefined;
  const regime = effective?.regime ?? raw?.regime;
  const universe = effective?.universe ?? [];
  const runs = agent?.runs ?? [];

  async function think() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.post<{ policy_id: string }>(
        "/api/v1/strategy/policies/generate",
        { purpose: "trade_runtime" },
      );
      setNotice(t("agent.decided", { id: shortId(result.policy_id, 8) }));
      await status.reload();
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setBusy(false);
    }
  }

  async function toggleRun(run: AgentRun) {
    setTraceError(null);
    if (openRun === run.id) {
      setOpenRun(null);
      return;
    }
    setOpenRun(run.id);
    // The newest run ships its trace in the list payload; older runs are
    // fetched once and kept so expanding them again is instant.
    if (run.tools.length > 0 || traces[run.id]) return;
    try {
      const detail = await api.get<AgentRunDetail>(
        `/api/v1/strategy/agent/${run.id}`,
      );
      setTraces((current) => ({ ...current, [run.id]: detail }));
    } catch (caught) {
      setTraceError((caught as ApiError).message);
    }
  }

  const shownTools = (run: AgentRun): AgentToolCall[] =>
    run.tools.length > 0 ? run.tools : traces[run.id]?.tools ?? [];

  const usageLine = (run: AgentRun): string | null => {
    const usage = run.usage ?? {};
    const input = Number(usage.input_tokens ?? usage.prompt_tokens ?? 0);
    const output = Number(usage.output_tokens ?? usage.completion_tokens ?? 0);
    if (!input && !output) return null;
    return t("agent.usage", { input, output });
  };

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="page-head">
        <div>
          <div className="page-kicker">
            {t("agent.kicker", { time: clock(view?.server_time) })}
          </div>
          <h1>{t("agent.title")}</h1>
          <ScribbleUnderline width={360} />
          <p className="lede">{t("agent.lede")}</p>
        </div>
        <button
          className="button"
          onClick={() => void think()}
          disabled={busy}
        >
          {busy ? t("agent.thinking") : t("agent.think")}
        </button>
      </div>

      {error && <ErrorNote error={error} />}
      {notice && <div className="callout olive">{notice}</div>}

      {agent && !agent.is_agent && (
        <div className="alert-band">
          <strong>{t("agent.simulatedTitle")}</strong>
          <p className="small" style={{ margin: "4px 0 0" }}>
            {t("agent.simulatedBody", { model: agent.model })}
          </p>
          <Link className="small" to="/">
            {t("agent.configureLink")}
          </Link>
        </div>
      )}

      <div className="agent-grid">
        <PaperCard
          eyebrow={t("agent.stance")}
          title={regime?.label ? label(regime.label) : t("agent.noDecision")}
        >
          <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            <Pill tone={policy?.simulated ? "terracotta" : "olive"} dot>
              {policy?.simulated
                ? t("agent.simulatedPolicy")
                : t("agent.authoredPolicy")}
            </Pill>
            {regime?.confidence !== undefined && (
              <span className="small muted">
                {t("agent.confidence", { value: pct(regime.confidence * 100, 0) })}
              </span>
            )}
            {policy?.generated_at && (
              <span className="small muted">
                {t("agent.authored", { time: timeAgo(policy.generated_at) })}
              </span>
            )}
            {policy?.expires_at && (
              <span className="small muted">
                {t("agent.expires", { time: timeAgo(policy.expires_at) })}
              </span>
            )}
          </div>

          <p className="agent-reason">
            {raw?.rationale ?? t("agent.noRationale")}
          </p>

          {universe.length > 0 && (
            <div className="chips" style={{ marginTop: 8 }}>
              {universe.map((item) => (
                <span className="chip" key={item.symbol}>
                  <span>{item.symbol.replace(/USDT$/, "")}</span>
                  <span className="small muted">
                    {t("agent.capShort", { value: item.max_position_notional_usdt })}
                  </span>
                </span>
              ))}
            </div>
          )}

          {policy?.clamps && policy.clamps.length > 0 && (
            <div className="clamp-list">
              <div className="eyebrow">{t("agent.clamps", { n: policy.clamps.length })}</div>
              {policy.clamps.slice(0, 5).map((clamp) => (
                <div className="small" key={`${clamp.field}-${clamp.reason}`}>
                  <span className="mono">{clamp.field}</span>{" "}
                  <span className="muted">
                    {clamp.requested} → {clamp.effective}
                  </span>
                </div>
              ))}
            </div>
          )}

          {raw?.invalidation?.length ? (
            <div className="small muted" style={{ marginTop: 10 }}>
              <strong>{t("agent.invalidation")}</strong>{" "}
              {raw.invalidation.slice(0, 3).join(" · ")}
            </div>
          ) : null}
        </PaperCard>

        <PaperCard eyebrow={t("agent.how")} title={t("agent.boundaries")}>
          <ol className="agent-steps">
            <li>{t("agent.stepRead")}</li>
            <li>{t("agent.stepDecide")}</li>
            <li>{t("agent.stepCompile")}</li>
            <li>{t("agent.stepExecute")}</li>
          </ol>
          <div className="eyebrow" style={{ marginTop: 14 }}>
            {t("agent.toolsTitle")}
          </div>
          <div className="tool-list">
            {(agent?.tools ?? []).map((name) => (
              <span className="chip mono" key={name}>
                {TOOL_LABELS[name] ? t(TOOL_LABELS[name]) : name}
              </span>
            ))}
          </div>
          <p className="small muted" style={{ marginTop: 12 }}>
            {t("agent.noExecution")}
          </p>
        </PaperCard>
      </div>

      <PaperCard
        eyebrow={t("agent.journal")}
        title={t("agent.journalTitle")}
        actions={
          <button
            className="button ghost small"
            onClick={() => void status.reload()}
          >
            {t("agent.refresh")}
          </button>
        }
      >
        {traceError && <div className="callout danger">{traceError}</div>}
        {runs.length === 0 ? (
          <EmptyState
            title={t("agent.noRuns")}
            hint={t("agent.noRunsHint")}
            action={
              <button
                className="button small"
                onClick={() => void think()}
                disabled={busy}
              >
                {t("agent.think")}
              </button>
            }
          />
        ) : (
          <div className="timeline">
            {runs.map((run) => {
              const tools = shownTools(run);
              const usage = usageLine(run);
              return (
                <div className="timeline-item" key={run.id}>
                  <div
                    className="timeline-dot"
                    style={{
                      borderColor:
                        run.status === "ok" ? "var(--olive)" : "var(--danger)",
                    }}
                  />
                  <div className="timeline-body">
                    <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
                      <strong>{clock(run.at)}</strong>
                      <Pill
                        tone={run.status === "ok" ? "olive" : "danger"}
                        dot
                      >
                        {run.status === "ok"
                          ? t("agent.ok")
                          : run.error_code ?? t("agent.failed")}
                      </Pill>
                      <span className="small muted">
                        {label(run.mode)} · {run.model}
                      </span>
                      <span className="small muted" style={{ marginLeft: "auto" }}>
                        {timeAgo(run.at)}
                      </span>
                    </div>

                    {run.rationale && (
                      <p className="agent-reason small">{run.rationale}</p>
                    )}

                    <div className="row small muted" style={{ flexWrap: "wrap", gap: 10 }}>
                      <span>{t("agent.turns", { n: run.turns })}</span>
                      <span>{t("agent.toolsUsed", { n: run.tool_count })}</span>
                      <span>{t("agent.signals", { n: run.signals.created })}</span>
                      {run.signals.rejected > 0 && (
                        <span>{t("agent.rejected", { n: run.signals.rejected })}</span>
                      )}
                      <span>{t("agent.closedTrades", { n: run.realized.closed })}</span>
                      <span
                        className={
                          Number(run.realized.pnl_usdt) < 0 ? "neg" : "pos"
                        }
                      >
                        {signed(run.realized.pnl_usdt)}
                      </span>
                      {usage && <span className="mono">{usage}</span>}
                      {run.tool_count > 0 && (
                        <button
                          type="button"
                          className="link-button"
                          onClick={() => void toggleRun(run)}
                        >
                          {openRun === run.id
                            ? t("agent.hideTrace")
                            : t("agent.showTrace")}
                        </button>
                      )}
                    </div>

                    {openRun === run.id && (
                      <div className="tool-trace">
                        {tools.length === 0 ? (
                          <div className="small muted">{t("agent.traceLoading")}</div>
                        ) : (
                          tools.map((call, index) => (
                            <div className="trace-row" key={`${call.name}-${index}`}>
                              <Pill tone={call.ok ? "olive" : "danger"} dot>
                                {TOOL_LABELS[call.name]
                                  ? t(TOOL_LABELS[call.name])
                                  : call.name}
                              </Pill>
                              <span className="small">
                                {call.summary || t("agent.noSummary")}
                              </span>
                              <span className="mono muted small">
                                {call.latency_ms} ms
                              </span>
                            </div>
                          ))
                        )}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </PaperCard>
    </div>
  );
}
