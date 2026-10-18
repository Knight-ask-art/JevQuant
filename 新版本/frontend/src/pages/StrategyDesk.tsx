import { useState } from "react";

import { ErrorNote, PaperCard, Pill, ScribbleUnderline } from "../components/ui";
import { useApi } from "../hooks/useApi";
import { useI18n } from "../i18n";
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
  const { t } = useI18n();
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
        t("strategy.notice", {
          id: shortId(result.policy_id),
          simulated: result.simulated ? t("strategy.noticeSimulated") : "",
        }),
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
          <div className="page-kicker">{t("strategy.kicker")}</div>
          <h1>{t("strategy.title")}</h1>
          <ScribbleUnderline width={300} />
          <p className="page-lede">{t("strategy.lede")}</p>
        </div>
        <div className="row">
          <div className="segmented">
            <button
              className={purpose === "trade_runtime" ? "active" : ""}
              onClick={() => setPurpose("trade_runtime")}
              title={t("strategy.consoleTitle")}
            >
              {t("strategy.consoleRuntime")}
            </button>
            <button
              className={purpose === "strategy_authoring" ? "active" : ""}
              onClick={() => setPurpose("strategy_authoring")}
              title={t("strategy.goTitle")}
            >
              {t("strategy.goAuthoring")}
            </button>
          </div>
          <button className="button" onClick={() => void generate()} disabled={busy}>
            {busy ? t("strategy.drafting") : t("strategy.generatePolicy")}
          </button>
        </div>
      </div>

      {error && <ErrorNote error={error} />}
      {notice && <div className="callout olive">{notice}</div>}

      {!policy ? (
        <PaperCard>
          <div className="empty">
            <div className="hand">{t("strategy.emptyTitle")}</div>
            <p className="small" style={{ marginTop: 6 }}>
              {t("strategy.emptyHint")}
            </p>
          </div>
        </PaperCard>
      ) : (
        <>
          <div className="grid grid-3">
            <PaperCard tint="paper">
              <div className="eyebrow">{t("strategy.regime")}</div>
              <h2 style={{ marginTop: 6 }}>{label(String(regime.label ?? "unknown"))}</h2>
              <p className="stat-note">
                {t("strategy.confidence", {
                  value: Number(regime.confidence ?? 0).toFixed(2),
                })}
              </p>
              <p className="small muted" style={{ marginTop: 8 }}>
                {String(regime.notes ?? "")}
              </p>
            </PaperCard>
            <PaperCard>
              <div className="eyebrow">{t("strategy.freshness")}</div>
              <h2 style={{ marginTop: 6 }}>{countdown(policy.expires_at)}</h2>
              <p className="stat-note">
                {t("strategy.generated", { time: clock(policy.generated_at) })}
              </p>
              <div className="row" style={{ marginTop: 10 }}>
                <Pill tone="yellow">{label(policy.status)}</Pill>
                <Pill tone={policy.simulated ? "terracotta" : "olive"}>
                  {policy.simulated ? t("common.simulated") : policy.source}
                </Pill>
              </div>
            </PaperCard>
            <PaperCard>
              <div className="eyebrow">{t("strategy.identity")}</div>
              <h2 style={{ marginTop: 6 }} className="mono">
                {shortId(policy.policy_id, 13)}
              </h2>
              <p className="stat-note mono">{shortId(policy.policy_hash, 24)}</p>
              <p className="small muted" style={{ marginTop: 8 }}>
                {t("strategy.scope", { value: policy.mode_scope?.join(", ") })}
              </p>
            </PaperCard>
          </div>

          <PaperCard
            eyebrow={t("strategy.rationale")}
            title={t("strategy.whyThisPolicy")}
            actions={
              <div className="tabs" style={{ border: "none", margin: 0 }}>
                <button
                  className={tab === "notebook" ? "active" : ""}
                  onClick={() => setTab("notebook")}
                >
                  {t("strategy.proposals")}
                </button>
                <button
                  className={tab === "compiled" ? "active" : ""}
                  onClick={() => setTab("compiled")}
                >
                  {t("strategy.compiled")}
                </button>
              </div>
            }
          >
            <p style={{ color: "var(--ink-soft)", maxWidth: 820 }}>
              {String(raw.rationale ?? t("strategy.noRationale"))}
            </p>
            <hr className="divider" />
            <div className="eyebrow" style={{ marginBottom: 8 }}>
              {tab === "notebook"
                ? t("strategy.universeModel")
                : t("strategy.universeServer")}
            </div>
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>{t("common.symbol")}</th>
                    <th>{t("strategy.sides")}</th>
                    <th className="num">{t("strategy.maxNotional")}</th>
                    <th className="num">{t("strategy.maxSpread")}</th>
                    <th className="num">{t("strategy.maxSlippage")}</th>
                    <th className="num">{t("strategy.cooldown")}</th>
                  </tr>
                </thead>
                <tbody>
                  {(tab === "notebook" ? rawUniverse : effectiveUniverse).map((item) => (
                    <tr key={String(item.symbol)}>
                      <td>
                        <strong>{String(item.symbol)}</strong>
                      </td>
                      <td className="mono">
                        {(Array.isArray(item.allowed_sides) ? item.allowed_sides : [])
                          .map((side) => label(String(side)))
                          .join(", ")}
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
                <div className="eyebrow">{t("strategy.portfolio")}</div>
                <div className="kv" style={{ marginTop: 6 }}>
                  <dt>{t("strategy.grossCap")}</dt>
                  <dd>{String(portfolio.max_gross_exposure_pct)}%</dd>
                  <dt>{t("strategy.maxPositions")}</dt>
                  <dd>{String(portfolio.max_positions)}</dd>
                  <dt>{t("strategy.dailyLoss")}</dt>
                  <dd>{String(portfolio.max_daily_loss_pct)}%</dd>
                  <dt>{t("strategy.drawdown")}</dt>
                  <dd>{String(portfolio.max_drawdown_pct)}%</dd>
                </div>
              </div>
              <div>
                <div className="eyebrow">{t("strategy.jevGate")}</div>
                <div className="kv" style={{ marginTop: 6 }}>
                  <dt>{t("strategy.choice")}</dt>
                  <dd>{label(String(gate.entry_choice))}</dd>
                  <dt>{t("strategy.minProbability")}</dt>
                  <dd>{String(gate.min_choice_probability)}</dd>
                  <dt>{t("strategy.minFit")}</dt>
                  <dd>{String(gate.min_policy_fit)}</dd>
                  <dt>{t("strategy.minQuality")}</dt>
                  <dd>{String(gate.min_quality_score)}</dd>
                </div>
              </div>
              <div>
                <div className="eyebrow">{t("strategy.exits")}</div>
                <div className="kv" style={{ marginTop: 6 }}>
                  <dt>{t("strategy.stopLoss")}</dt>
                  <dd>{String(exits.stop_loss_bps)} bps</dd>
                  <dt>{t("strategy.takeProfit")}</dt>
                  <dd>{String(exits.take_profit_bps)} bps</dd>
                  <dt>{t("strategy.maxHolding")}</dt>
                  <dd>{String(exits.max_holding_seconds)} s</dd>
                </div>
              </div>
            </div>

            {invalidation.length > 0 && (
              <>
                <hr className="divider" />
                <div className="eyebrow">{t("strategy.invalidationNotes")}</div>
                <ul className="small" style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                  {invalidation.map((item, index) => (
                    <li key={index}>{String(item)}</li>
                  ))}
                </ul>
              </>
            )}
          </PaperCard>

          <PaperCard
            eyebrow={t("strategy.compilerDiff")}
            title={
              policy.clamps.length === 1
                ? t("strategy.oneClamp")
                : t("strategy.clampsCount", { n: policy.clamps.length })
            }
          >
            {policy.clamps.length ? (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>{t("strategy.field")}</th>
                      <th>{t("strategy.requested")}</th>
                      <th>{t("strategy.effective")}</th>
                      <th>{t("strategy.reason")}</th>
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
              <p className="hand">{t("strategy.noClamps")}</p>
            )}
            <div className="callout" style={{ marginTop: 16 }}>
              {t("strategy.compilerNarrowing")}
            </div>
          </PaperCard>
        </>
      )}
    </div>
  );
}
