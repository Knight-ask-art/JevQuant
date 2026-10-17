import { useState } from "react";

import { ErrorNote, PaperCard, Pill, ScribbleUnderline } from "../components/ui";
import { useApi } from "../hooks/useApi";
import { ApiError, api } from "../lib/api";
import { label, shortId } from "../lib/format";
import { ExchangeBalances } from "./ExchangeBalances";

interface ConfigResponse {
  config: Record<string, unknown>;
  execution_backend: string;
  data_source: string;
}

export function ModelsCosts() {
  const config = useApi<ConfigResponse>("/api/v1/system/config", 30000);
  const [results, setResults] = useState<Record<string, unknown>>({});
  const [models, setModels] = useState<string[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function call(name: string, path: string, method: "GET" | "POST") {
    setBusy(name);
    setError(null);
    try {
      const result =
        method === "GET"
          ? await api.get<Record<string, unknown>>(path)
          : await api.post<Record<string, unknown>>(path);
      setResults((current) => ({ ...current, [name]: result }));
      if (name === "opencode-models" && Array.isArray(result.models)) {
        setModels(result.models as string[]);
      }
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setBusy(null);
    }
  }

  const summary = config.data?.config ?? {};
  const flags = [
    ["OpenCode Console", "opencode_console_configured"],
    ["OpenCode Go", "opencode_go_configured"],
    ["Jev", "jev_configured"],
    ["Hummingbot", "hummingbot_configured"],
    ["Binance live key", "binance_live_credentials_configured"],
    ["Binance testnet key", "binance_testnet_credentials_configured"],
    ["MFA TOTP", "mfa_configured"],
  ] as const;

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="page-head">
        <div>
          <div className="page-kicker">Models &amp; Costs</div>
          <h1>Who is allowed to think what</h1>
          <ScribbleUnderline width={340} />
          <p className="page-lede">
            OpenCode Console authors runtime strategy. OpenCode Go is a coding-agent provider:
            the server denies <span className="mono">trade_runtime</span> and{" "}
            <span className="mono">backtest_analysis</span> for it, regardless of UI state.
            Jev evaluates typed questions and never receives execution authority.
          </p>
        </div>
        <Pill tone={summary.synthetic_market ? "terracotta" : "olive"}>
          {summary.synthetic_market ? "simulated fallbacks" : "live providers"}
        </Pill>
      </div>

      {error && <ErrorNote error={error} />}

      <ExchangeBalances />

      <div className="grid grid-3">
        {flags.map(([name, key]) => (
          <PaperCard key={key} tint={summary[key] ? "olive" : undefined}>
            <div className="eyebrow">Connection</div>
            <h2 style={{ marginTop: 4 }}>{name}</h2>
            <div className="row" style={{ marginTop: 8 }}>
              <Pill tone={summary[key] ? "olive" : "yellow"} dot>
                {summary[key] ? "configured" : "not configured"}
              </Pill>
            </div>
          </PaperCard>
        ))}
      </div>

      <div className="two-col">
        <div className="stack">
          <PaperCard eyebrow="OpenCode Console" title="Runtime authoring">
            <p className="small" style={{ marginBottom: 12, color: "var(--ink-soft)" }}>
              Purpose <span className="mono">trade_runtime</span>. The adapter sends a strict
              schema instruction and validates the response against{" "}
              <span className="mono">strategy-policy/1.0</span> before the compiler sees it.
            </p>
            <div className="row">
              <button
                className="button small"
                onClick={() => void call("opencode-test", "/api/v1/connections/opencode/test", "POST")}
                disabled={busy !== null}
              >
                {busy === "opencode-test" ? "Testing…" : "Test connection"}
              </button>
              <button
                className="button ghost small"
                onClick={() =>
                  void call("opencode-models", "/api/v1/connections/opencode/models", "GET")
                }
                disabled={busy !== null}
              >
                {busy === "opencode-models" ? "Listing…" : "List models"}
              </button>
            </div>
            {models && (
              <div className="row" style={{ marginTop: 10 }}>
                {models.length ? (
                  models.slice(0, 12).map((model) => (
                    <Pill key={model}>{shortId(model, 24)}</Pill>
                  ))
                ) : (
                  <span className="hand">No catalogue — simulated author has no models.</span>
                )}
              </div>
            )}
            {"opencode-test" in results && (
              <pre className="mono" style={preStyle}>
                {JSON.stringify(results["opencode-test"], null, 2)}
              </pre>
            )}
          </PaperCard>

          <PaperCard eyebrow="Jev" title="Typed evaluator">
            <p className="small" style={{ marginBottom: 12, color: "var(--ink-soft)" }}>
              Jev returns a choice distribution, a 0–5 quality score, and a yes/no policy fit.
              Range validation happens on the server; malformed answers fail closed.
            </p>
            <button
              className="button small"
              onClick={() => void call("jev-test", "/api/v1/connections/jev/test", "POST")}
              disabled={busy !== null}
            >
              {busy === "jev-test" ? "Testing…" : "Test Jev"}
            </button>
            {"jev-test" in results && (
              <pre className="mono" style={preStyle}>
                {JSON.stringify(results["jev-test"], null, 2)}
              </pre>
            )}
          </PaperCard>

          <PaperCard eyebrow="Binance" title="Key restrictions">
            <p className="small" style={{ marginBottom: 12, color: "var(--ink-soft)" }}>
              The live gate requires reading + spot trading, withdrawals disabled, and IP
              restriction enabled. A restricted location answering HTTP 451 is reported, never
              bypassed.
            </p>
            <button
              className="button small"
              onClick={() => void call("binance-test", "/api/v1/connections/binance/test", "POST")}
              disabled={busy !== null}
            >
              {busy === "binance-test" ? "Inspecting…" : "Inspect key"}
            </button>
            {"binance-test" in results && (
              <pre className="mono" style={preStyle}>
                {JSON.stringify(results["binance-test"], null, 2)}
              </pre>
            )}
          </PaperCard>
        </div>

        <div className="stack">
          <PaperCard eyebrow="Redacted configuration" title="No secrets leave the server">
            <div className="kv">
              {Object.entries(summary).map(([key, value]) => (
                <FragmentRow key={key} name={key} value={value} />
              ))}
            </div>
          </PaperCard>

          <PaperCard eyebrow="Hummingbot" title="Execution gateway">
            <div className="row">
              <button
                className="button ghost small"
                onClick={() =>
                  void call("hummingbot", "/api/v1/connections/hummingbot/health", "GET")
                }
                disabled={busy !== null}
              >
                {busy === "hummingbot" ? "Checking…" : "Check gateway"}
              </button>
            </div>
            {"hummingbot" in results && (
              <pre className="mono" style={preStyle}>
                {JSON.stringify(results.hummingbot, null, 2)}
              </pre>
            )}
          </PaperCard>

          <PaperCard eyebrow="Cost posture" title="What this costs">
            <div className="note-list">
              <span className="ok">Paper mode: no exchange fees, no order routing.</span>
              <span className="warn">
                LLM and Jev calls cost money when configured; synthetic fallbacks do not.
              </span>
              <span className="warn">
                Backtests are local and synthetic by default; real data replay is a separate
                data-source decision.
              </span>
              <span className="ok">
                Live exits are deterministic — no model call is required to close a position.
              </span>
            </div>
          </PaperCard>

          {config.data && (
            <PaperCard eyebrow="Runtime" title="Data &amp; execution">
              <div className="kv">
                <dt>Data source</dt>
                <dd className="mono">{config.data.data_source}</dd>
                <dt>Execution backend</dt>
                <dd className="mono">{config.data.execution_backend}</dd>
                <dt>Environment</dt>
                <dd className="mono">{String(summary.app_env ?? "dev")}</dd>
                <dt>Auth disabled</dt>
                <dd>{String(summary.auth_disabled ?? true)}</dd>
              </div>
            </PaperCard>
          )}
        </div>
      </div>
    </div>
  );
}

const preStyle = {
  marginTop: 10,
  padding: 10,
  background: "var(--paper-2)",
  border: "1px dashed var(--line-strong)",
  borderRadius: 6,
  maxHeight: 220,
  overflow: "auto",
  whiteSpace: "pre-wrap" as const,
};

function FragmentRow({ name, value }: { name: string; value: unknown }) {
  if (value && typeof value === "object") {
    return (
      <>
        <dt>{label(name)}</dt>
        <dd className="mono small" style={{ whiteSpace: "pre-wrap" }}>
          {JSON.stringify(value)}
        </dd>
      </>
    );
  }
  return (
    <>
      <dt>{label(name)}</dt>
      <dd className="mono small">{String(value)}</dd>
    </>
  );
}
