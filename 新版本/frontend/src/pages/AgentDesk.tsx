import { useEffect, useState } from "react";
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
  get_recent_evaluations: "agent.tool.evaluations",
  get_prior_strategy: "agent.tool.prior",
};

/** Group the flat call list by agent turn; calls arrive in turn order. */
function byTurn(calls: AgentToolCall[]): Array<[number, AgentToolCall[]]> {
  const turns: Array<[number, AgentToolCall[]]> = [];
  for (const call of calls) {
    const turn = Number(call.turn ?? 1);
    const last = turns[turns.length - 1];
    if (last && last[0] === turn) last[1].push(call);
    else turns.push([turn, [call]]);
  }
  return turns;
}

function traceFlag(usage: Record<string, unknown>, flag: string): boolean {
  const trace = usage.__trace__;
  if (!trace || typeof trace !== "object") return false;
  return Boolean((trace as Record<string, unknown>)[flag]);
}

function ToolCallRow({ call }: { call: AgentToolCall }) {
  const { t } = useI18n();
  const known = TOOL_LABELS[call.name];
  const args = Object.entries(call.arguments ?? {})
    .map(([key, value]) => `${key}=${String(value)}`)
    .join(" ");
  return (
    <div
      className="trace-row"
      style={{ gridTemplateColumns: "minmax(0, 1fr) auto" }}
    >
      <div style={{ minWidth: 0 }}>
        <Pill tone={call.ok ? "olive" : "danger"} dot>
          {known ? t(known) : call.name}
        </Pill>{" "}
        <span className="small">{call.summary || t("agent.noSummary")}</span>
        {args && (
          <div className="mono muted small" title={t("agent.callArgs")}>
            {args}
          </div>
        )}
      </div>
      <span className="mono muted small">{call.latency_ms} ms</span>
    </div>
  );
}

export function AgentDesk() {
  const { t } = useI18n();
  const status = useApi<SystemStatus>("/api/v1/system/status", 5000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [limit, setLimit] = useState(10);
  const [traces, setTraces] = useState<Record<string, AgentRunDetail>>({});
  const [traceError, setTraceError] = useState<string | null>(null);

  const view = status.data;
  const agent = view?.agent;
  const policy = view?.policy;
  const raw = policy?.raw as RawPolicy | undefined;
  const effective = policy?.effective as EffectivePolicy | undefined;
  const regime = effective?.regime ?? raw?.regime;
  const universe = effective?.universe ?? [];
  const runs = (agent?.runs ?? []).slice(0, limit);
  const newest = runs[0];

  async function think() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      // The authoring loop can take minutes; a dropped socket must not read
      // as a failed decision.
      const result = await api.post<{ policy_id: string }>(
        "/api/v1/strategy/policies/generate",
        { purpose: "trade_runtime" },
        { timeoutMs: 300_000 },
      );
      setNotice(t("agent.decided", { id: shortId(result.policy_id, 8) }));
      await status.reload();
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setBusy(false);
    }
  }

  function loadTrace(run: AgentRun) {
    // The newest run ships its trace in the list payload; older runs are
    // fetched once and kept so the narration renders instantly next time.
    if (run.tools.length > 0 || traces[run.id]) return;
    api
      .get<AgentRunDetail>(`/api/v1/strategy/agent/${run.id}`)
      .then((detail) =>
        setTraces((current) => ({ ...current, [run.id]: detail })),
      )
      .catch((caught: ApiError) => setTraceError(caught.message));
  }

  const shownTools = (run: AgentRun): AgentToolCall[] =>
    run.tools.length > 0 ? run.tools : traces[run.id]?.tools ?? [];

  const usageLine = (run: AgentRun): string | null => {
    const usage = run.usage ?? {};
    const input = Number(usage.input_tokens ?? usage.prompt_tokens ?? 0);
    const output = Number(usage.output_tokens ?? usage.completion_tokens ?? 0);
    if (!input && !output) return null;
    const line = t("agent.usage", { input, output });
    // Present only when a per-token price is configured; otherwise the run
    // shows its tokens alone rather than a misleading $0.00.
    const cost = typeof usage.__cost_usd__ === "string" ? usage.__cost_usd__ : null;
    if (!cost || Number(cost) === 0) return line;
    return t("agent.usageCost", { input, output, cost: Number(cost).toFixed(4) });
  };

  // Pull the newest run's trace once so the narration is there on arrival.
  const newestId = newest?.id;
  const newestLoaded =
    !newest || newest.tools.length > 0 || newest.tool_count === 0;
  useEffect(() => {
    if (newest && !newestLoaded) loadTrace(newest);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [newestId, newestLoaded]);

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
          aria-busy={busy}
        >
          {busy ? t("agent.thinking") : t("agent.think")}
        </button>
      </div>

      {error && <ErrorNote error={error} />}
      {busy && (
        <div className="callout" role="status" aria-live="polite">
          {t("agent.announceStart")}
        </div>
      )}
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
              const turns = byTurn(tools);
              const usage = usageLine(run);
              const repaired = traceFlag(run.usage ?? {}, "repaired");
              const forced = traceFlag(run.usage ?? {}, "forced_answer");
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
                      {repaired && (
                        <Pill tone="yellow">{t("agent.repaired")}</Pill>
                      )}
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

                    <div className="agent-narration">
                      <div className="eyebrow">{t("agent.narration")}</div>
                      {turns.map(([turn, calls]) => (
                        <div className="agent-turn" key={turn}>
                          <div className="agent-turn-head small">
                            {t("agent.turnRead", {
                              n: turn,
                              n_tools: calls.length,
                            })}
                          </div>
                          <div className="tool-trace">
                            {calls.map((call, index) => (
                              <ToolCallRow
                                key={`${call.name}-${index}`}
                                call={call}
                              />
                            ))}
                          </div>
                        </div>
                      ))}
                      <div className="agent-turn">
                        {forced && (
                          <div className="small muted">{t("agent.turnForced")}</div>
                        )}
                        {repaired && (
                          <div className="small muted">{t("agent.turnRepair")}</div>
                        )}
                        <div className="agent-turn-head small">
                          {t("agent.turnWrite", { n: run.turns })}
                        </div>
                      </div>
                      {tools.length === 0 && run.tool_count > 0 && (
                        <div className="small muted">{t("agent.traceLoading")}</div>
                      )}
                    </div>

                    <div className="row small muted" style={{ flexWrap: "wrap", gap: 10 }}>
                      <span>{t("agent.turns", { n: run.turns })}</span>
                      <span>{t("agent.toolsUsed", { n: run.tool_count })}</span>
                      <span>
                        {t("agent.elapsed", {
                          seconds: (run.latency_ms / 1000).toFixed(1),
                        })}
                      </span>
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
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
        {runs.length > 0 && (
          <button
            className="button ghost small"
            style={{ marginTop: 12 }}
            onClick={() => setLimit((current) => current + 20)}
          >
            {t("agent.showMore")}
          </button>
        )}
      </PaperCard>

    </div>
  );
}
