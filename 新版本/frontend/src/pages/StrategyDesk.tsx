import { useState } from "react";

import { ErrorNote, PaperCard, Pill, ScribbleUnderline } from "../components/ui";
import { useApi } from "../hooks/useApi";
import { ApiError, api } from "../lib/api";
import { clock, countdown, label, shortId } from "../lib/format";
import type { PolicyView } from "../lib/types";

interface CurrentPolicy {
  active: boolean;
  compiled: Record<string, unknown> | null;
  policy: (PolicyView & { mode_scope: string[] }) | null;
}

type Tab = "notebook" | "compiled";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function asArray(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? (value as Array<Record<string, unknown>>) : [];
}

export function StrategyDesk() {
  const current = useApi<CurrentPolicy>("/api/v1/strategy/policies/current", 15000);
  const [purpose, setPurpose] = useState<"trade_runtime" | "strategy_authoring">(
    "trade_runtime",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [tab, setTab] = useState<Tab>("notebook");
  const [notice, setNotice] = useState<string | null>(null);

  const policy = current.data?.policy ?? null;
  const raw = asRecord(policy?.raw);
  const effective = asRecord(policy?.effective);
  const regime = asRecord(raw.regime);
  const rawUniverse = asArray(raw.universe);
  const effectiveUniverse = asArray(effective.universe);
  const portfolio = asRecord(raw.portfolio);
  const gate = asRecord(raw.jev_gate);
  const exits = asRecord(raw.exit_defaults);
  const invalidation = Array.isArray(raw.invalidation) ? raw.invalidation : [];

  async function generate() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.post<{ policy_id: string; simulated: boolean }>(
        "/api/v1/strategy/policies/generate",
        { purpose },
      );
      setNotice(
        `Policy ${shortId(result.policy_id)} compiled${result.simulated ? " (simulated author)" : ""}.`,
      );
      await current.reload();
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="page-head">
        <div>
          <div className="page-kicker">Strategy Desk</div>
          <h1>The strategy notebook</h1>
          <ScribbleUnderline width={300} />
          <p className="page-lede">
            The model writes a proposal. The compiler rewrites it against the hard server
            envelope, field by field. Only the compiled policy reaches Jev, risk and execution.
          </p>
        </div>
        <div className="row">
          <div className="segmented">
            <button
              className={purpose === "trade_runtime" ? "active" : ""}
              onClick={() => setPurpose("trade_runtime")}
              title="OpenCode Console path for runtime strategy"
            >
              Console · runtime
            </button>
            <button
              className={purpose === "strategy_authoring" ? "active" : ""}
              onClick={() => setPurpose("strategy_authoring")}
              title="Authoring path; OpenCode Go permits this purpose"
            >
              Go · authoring
            </button>
          </div>
          <button className="button" onClick={() => void generate()} disabled={busy}>
            {busy ? "Drafting…" : "Generate policy"}
          </button>
        </div>
      </div>

      {error && <ErrorNote error={error} />}
      {notice && <div className="callout olive">{notice}</div>}

      {!policy ? (
        <PaperCard>
          <div className="empty">
            <div className="hand">The desk is empty.</div>
            <p className="small" style={{ marginTop: 6 }}>
              Generate a policy or run a cycle; the orchestrator asks the configured author
              automatically when no fresh policy exists.
            </p>
          </div>
        </PaperCard>
      ) : (
        <>
          <div className="grid grid-3">
            <PaperCard tint="paper">
              <div className="eyebrow">Regime</div>
              <h2 style={{ marginTop: 6 }}>{label(String(regime.label ?? "unknown"))}</h2>
              <p className="stat-note">
                confidence {Number(regime.confidence ?? 0).toFixed(2)}
              </p>
              <p className="small muted" style={{ marginTop: 8 }}>
                {String(regime.notes ?? "")}
              </p>
            </PaperCard>
            <PaperCard>
              <div className="eyebrow">Freshness</div>
              <h2 style={{ marginTop: 6 }}>{countdown(policy.expires_at)}</h2>
              <p className="stat-note">generated {clock(policy.generated_at)}</p>
              <div className="row" style={{ marginTop: 10 }}>
                <Pill tone="yellow">{policy.status}</Pill>
                <Pill tone={policy.simulated ? "terracotta" : "olive"}>
                  {policy.simulated ? "simulated" : policy.source}
                </Pill>
              </div>
            </PaperCard>
            <PaperCard>
              <div className="eyebrow">Identity</div>
              <h2 style={{ marginTop: 6 }} className="mono">
                {shortId(policy.policy_id, 13)}
              </h2>
              <p className="stat-note mono">{shortId(policy.policy_hash, 24)}</p>
              <p className="small muted" style={{ marginTop: 8 }}>
                scope · {policy.mode_scope?.join(", ")}
              </p>
            </PaperCard>
          </div>

          <PaperCard
            eyebrow="Rationale"
            title="Why this policy"
            actions={
              <div className="tabs" style={{ border: "none", margin: 0 }}>
                <button
                  className={tab === "notebook" ? "active" : ""}
                  onClick={() => setTab("notebook")}
                >
                  Proposals
                </button>
                <button
                  className={tab === "compiled" ? "active" : ""}
                  onClick={() => setTab("compiled")}
                >
                  Compiled
                </button>
              </div>
            }
          >
            <p style={{ color: "var(--ink-soft)", maxWidth: 820 }}>
              {String(raw.rationale ?? "No rationale was returned.")}
            </p>
            <hr className="divider" />
            <div className="eyebrow" style={{ marginBottom: 8 }}>
              Universe · {tab === "notebook" ? "model proposal" : "after server envelope"}
            </div>
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Sides</th>
                    <th className="num">Max notional</th>
                    <th className="num">Max spread (bps)</th>
                    <th className="num">Max slippage (bps)</th>
                    <th className="num">Cooldown (s)</th>
                  </tr>
                </thead>
                <tbody>
                  {(tab === "notebook" ? rawUniverse : effectiveUniverse).map((item) => (
                    <tr key={String(item.symbol)}>
                      <td>
                        <strong>{String(item.symbol)}</strong>
                      </td>
                      <td className="mono">
                        {(Array.isArray(item.allowed_sides) ? item.allowed_sides : []).join(
                          ", ",
                        )}
                      </td>
                      <td className="num">{String(item.max_position_notional_usdt)}</td>
                      <td className="num">{String(item.max_spread_bps)}</td>
                      <td className="num">{String(item.max_slippage_bps)}</td>
                      <td className="num">{String(item.entry_cooldown_seconds)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="grid grid-3" style={{ marginTop: 18 }}>
              <div>
                <div className="eyebrow">Portfolio</div>
                <div className="kv" style={{ marginTop: 6 }}>
                  <dt>Gross cap</dt>
                  <dd>{String(portfolio.max_gross_exposure_pct)}%</dd>
                  <dt>Max positions</dt>
                  <dd>{String(portfolio.max_positions)}</dd>
                  <dt>Daily loss</dt>
                  <dd>{String(portfolio.max_daily_loss_pct)}%</dd>
                  <dt>Drawdown</dt>
                  <dd>{String(portfolio.max_drawdown_pct)}%</dd>
                </div>
              </div>
              <div>
                <div className="eyebrow">Jev gate</div>
                <div className="kv" style={{ marginTop: 6 }}>
                  <dt>Choice</dt>
                  <dd>{String(gate.entry_choice)}</dd>
                  <dt>Min probability</dt>
                  <dd>{String(gate.min_choice_probability)}</dd>
                  <dt>Min fit</dt>
                  <dd>{String(gate.min_policy_fit)}</dd>
                  <dt>Min quality</dt>
                  <dd>{String(gate.min_quality_score)}</dd>
                </div>
              </div>
              <div>
                <div className="eyebrow">Deterministic exits</div>
                <div className="kv" style={{ marginTop: 6 }}>
                  <dt>Stop loss</dt>
                  <dd>{String(exits.stop_loss_bps)} bps</dd>
                  <dt>Take profit</dt>
                  <dd>{String(exits.take_profit_bps)} bps</dd>
                  <dt>Max holding</dt>
                  <dd>{String(exits.max_holding_seconds)} s</dd>
                </div>
              </div>
            </div>

            {invalidation.length > 0 && (
              <>
                <hr className="divider" />
                <div className="eyebrow">Invalidation notes</div>
                <ul className="small" style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                  {invalidation.map((item, index) => (
                    <li key={index}>{String(item)}</li>
                  ))}
                </ul>
              </>
            )}
          </PaperCard>

          <PaperCard
            eyebrow="Compiler diff"
            title={`${policy.clamps.length} clamp${policy.clamps.length === 1 ? "" : "s"} applied`}
          >
            {policy.clamps.length ? (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Field</th>
                      <th>Requested</th>
                      <th>Effective</th>
                      <th>Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {policy.clamps.map((clamp) => (
                      <tr key={clamp.field}>
                        <td className="mono">{clamp.field}</td>
                        <td className="mono" style={{ color: "var(--terracotta)" }}>
                          {clamp.requested}
                        </td>
                        <td className="mono" style={{ color: "var(--olive)" }}>
                          {clamp.effective}
                        </td>
                        <td className="muted">{clamp.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="hand">The model stayed inside the envelope. No clamps.</p>
            )}
            <div className="callout" style={{ marginTop: 16 }}>
              The compiler can only narrow. A model that proposes a wider notional, spread,
              slippage, cooldown or TTL is corrected here — never trusted downstream.
            </div>
          </PaperCard>
        </>
      )}
    </div>
  );
}
