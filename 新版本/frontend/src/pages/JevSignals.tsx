import { useState } from "react";
import { Link } from "react-router-dom";

import { LifecycleTrail, ProbabilitySketch } from "../components/charts";
import { EmptyState, ErrorNote, PaperCard, Pill, ScribbleUnderline } from "../components/ui";
import { useApi } from "../hooks/useApi";
import {
  clock,
  label,
  money,
  shortId,
  signed,
  timeAgo,
  toneForState,
} from "../lib/format";
import type { SignalEvent, SignalState, SignalView } from "../lib/types";

interface JevEvaluation {
  id: string;
  model: string;
  answers: Record<string, unknown>;
  latency_ms: number;
  error_code: string | null;
  created_at: string;
}

interface SignalDetail {
  signal: SignalView;
  events: SignalEvent[];
  jev_evaluation: JevEvaluation | null;
  intent: Record<string, unknown> | null;
  risk_decision: {
    approved: boolean;
    failed_rules: Array<{ rule: string; message: string }>;
    effective: Record<string, string>;
    decision_hash: string;
  } | null;
  order: Record<string, unknown> | null;
}

const STATES: Array<SignalState | ""> = [
  "",
  "ELIGIBLE",
  "APPROVED",
  "FILLED",
  "POSITION_OPEN",
  "REJECTED",
  "RISK_REJECTED",
  "EXPIRED",
  "ERROR",
];

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

export function JevSignals() {
  const [state, setState] = useState<SignalState | "">("");
  const [selected, setSelected] = useState<string | null>(null);
  const list = useApi<{ items: SignalView[] }>(
    `/api/v1/signals?limit=100${state ? `&state=${state}` : ""}`,
    12000,
  );
  const detail = useApi<SignalDetail>(
    selected ? `/api/v1/signals/${selected}` : null,
    0,
  );

  const signals = list.data?.items ?? [];
  const chosen = detail.data;

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="page-head">
        <div>
          <div className="page-kicker">Jev Signals</div>
          <h1>Typed questions, signed answers</h1>
          <ScribbleUnderline width={320} />
          <p className="page-lede">
            Jev answers three typed questions — a choice with probabilities, a quality score,
            and a yes/no policy fit. It cannot size an order or change a limit. The gate and
            risk engine decide what happens next.
          </p>
        </div>
        <div className="row">
          <Link className="button ghost small" to="/risk">
            Gate thresholds
          </Link>
        </div>
      </div>

      <ErrorNote error={list.error} />

      <div className="row">
        <span className="eyebrow">Filter</span>
        <div className="segmented">
          {STATES.map((value) => (
            <button
              key={value || "all"}
              className={state === value ? "active" : ""}
              onClick={() => {
                setState(value);
                setSelected(null);
              }}
            >
              {value ? label(value) : "All"}
            </button>
          ))}
        </div>
      </div>

      <div className="two-col">
        <PaperCard eyebrow="Tape" title={`${signals.length} signals`}>
          <div className="signal-tape">
          {list.loading && !list.data ? (
            <div className="loading">Fetching signals…</div>
          ) : signals.length ? (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>State</th>
                    <th className="num">Buy / fit</th>
                    <th>Age</th>
                    <th className="num">Signal</th>
                  </tr>
                </thead>
                <tbody>
                  {signals.map((signal) => (
                    <tr
                      key={signal.id}
                      className="clickable"
                      onClick={() => setSelected(signal.id)}
                      style={{
                        background:
                          selected === signal.id ? "var(--paper-2)" : undefined,
                      }}
                    >
                      <td>
                        <strong>{signal.symbol}</strong>
                        <div className="muted small">{signal.side}</div>
                      </td>
                      <td>
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
                      </td>
                      <td className="num mono">
                        {Number(signal.score?.entry_probability ?? 0).toFixed(2)} /{" "}
                        {Number(signal.score?.policy_fit ?? 0).toFixed(2)}
                      </td>
                      <td className="muted">{timeAgo(signal.created_at)}</td>
                      <td className="num mono muted">{shortId(signal.id, 6)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState
              title="The tape is blank."
              hint="Run a cycle on the Overview page to create signals."
            />
          )}
          </div>
        </PaperCard>

        <div className="stack">
          {!selected ? (
            <PaperCard>
              <EmptyState
                title="Pick a signal"
                hint="The lifecycle trail, typed answers and the risk verdict appear here."
              />
            </PaperCard>
          ) : detail.error ? (
            <ErrorNote error={detail.error} />
          ) : !chosen ? (
            <PaperCard>
              <div className="loading">Opening the signal…</div>
            </PaperCard>
          ) : (
            <SelectedSignal detail={chosen} />
          )}
        </div>
      </div>
    </div>
  );
}

function SelectedSignal({ detail }: { detail: SignalDetail }) {
  const signal = detail.signal;
  const evaluation = detail.jev_evaluation;
  const answers = record(evaluation?.answers);
  const action = record(answers.entry_action);
  const quality = record(answers.entry_quality);
  const fit = record(answers.policy_fit);
  const probabilities = record(action.probabilities);
  const buy = Number(probabilities.buy ?? 0);
  const sell = Number(probabilities.sell ?? 0);
  const hold = Number(probabilities.hold ?? 0);
  const intent = detail.intent;
  const risk = detail.risk_decision;
  const order = detail.order;

  return (
    <>
      <PaperCard>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <div className="eyebrow">Signal {shortId(signal.id)}</div>
            <h2>{signal.symbol}</h2>
          </div>
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
        <ProbabilitySketch
          buy={buy || Number(signal.score?.entry_probability ?? 0)}
          sell={sell}
          hold={hold}
          fit={Number(fit.noul ?? signal.score?.policy_fit ?? 0)}
        />
        <div className="grid grid-3" style={{ marginTop: 10 }}>
          <div>
            <div className="stat-label">Choice</div>
            <div className="mono">{String(action.choice ?? signal.score?.entry_choice ?? "—")}</div>
          </div>
          <div>
            <div className="stat-label">Quality</div>
            <div className="mono">{String(quality.score ?? signal.score?.quality_score ?? "—")}</div>
          </div>
          <div>
            <div className="stat-label">Policy fit</div>
            <div className="mono">{fit.noul === undefined ? "—" : fit.noul ? "yes" : "no"}</div>
          </div>
        </div>
        {Boolean(quality.rationale) && (
          <p className="small" style={{ marginTop: 10, color: "var(--ink-soft)" }}>
            {String(quality.rationale)}
          </p>
        )}
        {evaluation?.error_code && (
          <div className="callout danger" style={{ marginTop: 10 }}>
            Jev error: {evaluation.error_code} — the signal fails closed.
          </div>
        )}
      </PaperCard>

      <PaperCard eyebrow="Lifecycle" title="State trail">
        <LifecycleTrail events={detail.events} />
        {!detail.events.length && <p className="hand">No events recorded.</p>}
      </PaperCard>

      {intent && (
        <PaperCard eyebrow="Intent" title="Bounded by risk">
          <div className="kv">
            <dt>Intent</dt>
            <dd className="mono">{shortId(String(intent.id))}</dd>
            <dt>Notional</dt>
            <dd className="mono">{money(intent.quote_notional as string)} USDT</dd>
            <dt>Limit</dt>
            <dd className="mono">{money(intent.limit_price as string)}</dd>
            <dt>Status</dt>
            <dd>
              <Pill tone="yellow">{String(intent.status)}</Pill>
            </dd>
            <dt>Idempotency key</dt>
            <dd className="mono small">{String(intent.idempotency_key)}</dd>
          </div>
        </PaperCard>
      )}

      {risk && (
        <PaperCard eyebrow="Risk verdict" title={risk.approved ? "Approved" : "Rejected"}>
          <div className="note-list">
            {risk.approved ? (
              <span className="ok">All deterministic checks passed.</span>
            ) : (
              risk.failed_rules.map((failure) => (
                <span className="bad" key={failure.rule}>
                  <span className="mono">{failure.rule}</span> — {failure.message}
                </span>
              ))
            )}
          </div>
          {risk.approved && (
            <div className="kv" style={{ marginTop: 12 }}>
              <dt>Effective notional</dt>
              <dd className="mono">{risk.effective.quote_notional} USDT</dd>
              <dt>Approved qty</dt>
              <dd className="mono">{risk.effective.approved_qty}</dd>
              <dt>Decision hash</dt>
              <dd className="mono small">{shortId(risk.decision_hash, 20)}</dd>
            </div>
          )}
        </PaperCard>
      )}

      {order && (
        <PaperCard eyebrow="Execution" title="Order">
          <div className="kv">
            <dt>Order</dt>
            <dd className="mono">{shortId(String(order.id))}</dd>
            <dt>Status</dt>
            <dd>{String(order.status)}</dd>
            <dt>Filled</dt>
            <dd className="mono">
              {String(order.filled_qty)} @ {money(order.avg_fill_price as string)}
            </dd>
            <dt>Fee</dt>
            <dd className="mono">{signed(-Number(order.fee_quote ?? 0), 4)}</dd>
            <dt>Created</dt>
            <dd>{clock(String(order.created_at))}</dd>
          </div>
        </PaperCard>
      )}
    </>
  );
}
