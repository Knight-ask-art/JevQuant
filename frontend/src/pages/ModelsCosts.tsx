import { useState } from "react";

import { ErrorNote, PaperCard, Pill, ScribbleUnderline } from "../components/ui";
import { useApi } from "../hooks/useApi";
import { useI18n } from "../i18n";
import { ApiError, api } from "../lib/api";
import { label, shortId } from "../lib/format";
import { ExchangeBalances } from "./ExchangeBalances";

interface ConfigResponse {
  config: Record<string, unknown>;
  execution_backend: string;
  data_source: string;
}

export function ModelsCosts() {
  const { t } = useI18n();
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
    ["models.flagOpenCodeConsole", "opencode_console_configured"],
    ["models.flagOpenCodeGo", "opencode_go_configured"],
    ["models.flagJev", "jev_configured"],
    ["models.flagHummingbot", "hummingbot_configured"],
    ["models.flagBinanceLive", "binance_live_credentials_configured"],
    ["models.flagBinanceTestnet", "binance_testnet_credentials_configured"],
    ["models.flagMfa", "mfa_configured"],
  ] as const;

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="page-head">
        <div>
          <div className="page-kicker">{t("models.kicker")}</div>
          <h1>{t("models.title")}</h1>
          <ScribbleUnderline width={340} />
          <p className="page-lede">{t("models.lede")}</p>
        </div>
        <Pill tone={summary.synthetic_market ? "terracotta" : "olive"}>
          {summary.synthetic_market
            ? t("models.simulatedFallbacks")
            : t("models.liveProviders")}
        </Pill>
      </div>

      {error && <ErrorNote error={error} />}

      <ExchangeBalances />

      <div className="grid grid-3">
        {flags.map(([nameKey, key]) => (
          <PaperCard key={key} tint={summary[key] ? "olive" : undefined}>
            <div className="eyebrow">{t("models.connection")}</div>
            <h2 style={{ marginTop: 4 }}>{t(nameKey)}</h2>
            <div className="row" style={{ marginTop: 8 }}>
              <Pill tone={summary[key] ? "olive" : "yellow"} dot>
                {summary[key] ? t("common.configured") : t("common.notConfigured")}
              </Pill>
            </div>
          </PaperCard>
        ))}
      </div>

      <div className="two-col">
        <div className="stack">
          <PaperCard eyebrow="OpenCode Console" title={t("models.runtimeAuthoring")}>
            <p className="small" style={{ marginBottom: 12, color: "var(--ink-soft)" }}>
              {t("models.runtimeAuthoringBody")}
            </p>
            <div className="row">
              <button
                className="button small"
                onClick={() => void call("opencode-test", "/api/v1/connections/opencode/test", "POST")}
                disabled={busy !== null}
              >
                {busy === "opencode-test" ? t("models.testing") : t("models.testConnection")}
              </button>
              <button
                className="button ghost small"
                onClick={() =>
                  void call("opencode-models", "/api/v1/connections/opencode/models", "GET")
                }
                disabled={busy !== null}
              >
                {busy === "opencode-models" ? t("models.listing") : t("models.listModels")}
              </button>
            </div>
            {models && (
              <div className="row" style={{ marginTop: 10 }}>
                {models.length ? (
                  models.slice(0, 12).map((model) => (
                    <Pill key={model}>{shortId(model, 24)}</Pill>
                  ))
                ) : (
                  <span className="hand">{t("models.noCatalogue")}</span>
                )}
              </div>
            )}
            {"opencode-test" in results && (
              <pre className="mono" style={preStyle}>
                {JSON.stringify(results["opencode-test"], null, 2)}
              </pre>
            )}
          </PaperCard>

          <PaperCard eyebrow="Jev" title={t("models.typedEvaluator")}>
            <p className="small" style={{ marginBottom: 12, color: "var(--ink-soft)" }}>
              {t("models.typedEvaluatorBody")}
            </p>
            <button
              className="button small"
              onClick={() => void call("jev-test", "/api/v1/connections/jev/test", "POST")}
              disabled={busy !== null}
            >
              {busy === "jev-test" ? t("models.testing") : t("models.testJev")}
            </button>
            {"jev-test" in results && (
              <pre className="mono" style={preStyle}>
                {JSON.stringify(results["jev-test"], null, 2)}
              </pre>
            )}
          </PaperCard>

          <PaperCard eyebrow="Binance" title={t("models.keyRestrictions")}>
            <p className="small" style={{ marginBottom: 12, color: "var(--ink-soft)" }}>
              {t("models.keyRestrictionsBody")}
            </p>
            <button
              className="button small"
              onClick={() => void call("binance-test", "/api/v1/connections/binance/test", "POST")}
              disabled={busy !== null}
            >
              {busy === "binance-test" ? t("models.inspecting") : t("models.inspectKey")}
            </button>
            {"binance-test" in results && (
              <pre className="mono" style={preStyle}>
                {JSON.stringify(results["binance-test"], null, 2)}
              </pre>
            )}
          </PaperCard>
        </div>

        <div className="stack">
          <PaperCard
            eyebrow={t("models.redactedConfig")}
            title={t("models.noSecrets")}
          >
            <div className="kv">
              {Object.entries(summary).map(([key, value]) => (
                <FragmentRow key={key} name={key} value={value} />
              ))}
            </div>
          </PaperCard>

          <PaperCard eyebrow="Hummingbot" title={t("models.executionGateway")}>
            <div className="row">
              <button
                className="button ghost small"
                onClick={() =>
                  void call("hummingbot", "/api/v1/connections/hummingbot/health", "GET")
                }
                disabled={busy !== null}
              >
                {busy === "hummingbot" ? t("models.checking") : t("models.checkGateway")}
              </button>
            </div>
            {"hummingbot" in results && (
              <pre className="mono" style={preStyle}>
                {JSON.stringify(results.hummingbot, null, 2)}
              </pre>
            )}
          </PaperCard>

          <PaperCard eyebrow={t("models.costPosture")} title={t("models.whatCosts")}>
            <div className="note-list">
              <span className="ok">{t("models.costPaper")}</span>
              <span className="warn">{t("models.costCalls")}</span>
              <span className="warn">{t("models.costBacktests")}</span>
              <span className="ok">{t("models.costExits")}</span>
            </div>
          </PaperCard>

          {config.data && (
            <PaperCard eyebrow={t("models.runtime")} title={t("models.dataExecution")}>
              <div className="kv">
                <dt>{t("models.dataSource")}</dt>
                <dd className="mono">{label(config.data.data_source)}</dd>
                <dt>{t("models.executionBackend")}</dt>
                <dd className="mono">{label(config.data.execution_backend)}</dd>
                <dt>{t("models.environment")}</dt>
                <dd className="mono">{String(summary.app_env ?? "dev")}</dd>
                <dt>{t("models.authDisabled")}</dt>
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
