import { useEffect, useState } from "react";

import { useI18n } from "../i18n";
import { ApiError, api } from "../lib/api";
import type { SystemStatus } from "../lib/types";
import { ErrorNote, Field, Pill } from "./ui";

interface HealthLine {
  healthy?: boolean;
  error?: string;
  provider?: string;
  model?: string;
}

interface SaveResult {
  models: SystemStatus["models"];
  health: { llm: HealthLine; jev: HealthLine };
}

export function ModelSettingsModal({
  open,
  models,
  onClose,
  onSaved,
}: {
  open: boolean;
  models: SystemStatus["models"] | undefined;
  onClose: () => void;
  onSaved: (models: SystemStatus["models"]) => void;
}) {
  const { t } = useI18n();
  const [llmBaseUrl, setLlmBaseUrl] = useState("");
  const [llmModel, setLlmModel] = useState("");
  const [llmProtocol, setLlmProtocol] = useState("openai");
  const [llmKey, setLlmKey] = useState("");
  const [llmClear, setLlmClear] = useState(false);
  const [jevBaseUrl, setJevBaseUrl] = useState("");
  const [jevModel, setJevModel] = useState("");
  const [jevPath, setJevPath] = useState("/v1/decisions");
  const [jevKey, setJevKey] = useState("");
  const [jevClear, setJevClear] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setLlmBaseUrl(models?.llm.base_url ?? "");
    setLlmModel(models?.llm.model ?? "");
    setLlmProtocol(models?.llm.protocol ?? "openai");
    setLlmKey("");
    setLlmClear(false);
    setJevBaseUrl(models?.jev.base_url ?? "");
    setJevModel(models?.jev.model ?? "");
    setJevPath(models?.jev.path ?? "/v1/decisions");
    setJevKey("");
    setJevClear(false);
    setError(null);
    setNotice(null);
    setWarning(null);
  }, [open, models]);

  if (!open) return null;

  async function save() {
    setBusy(true);
    setError(null);
    setNotice(null);
    setWarning(null);
    try {
      const result = await api.put<SaveResult>("/api/v1/settings/models", {
        llm: {
          base_url: llmBaseUrl,
          model: llmModel,
          protocol: llmProtocol,
          ...(llmKey ? { api_key: llmKey } : {}),
          ...(llmClear ? { clear_api_key: true } : {}),
        },
        jev: {
          base_url: jevBaseUrl,
          model: jevModel,
          path: jevPath,
          ...(jevKey ? { api_key: jevKey } : {}),
          ...(jevClear ? { clear_api_key: true } : {}),
        },
      });
      onSaved(result.models);
      setNotice(t("cockpit.modelsSaved"));
      setLlmKey("");
      setJevKey("");
      setLlmClear(false);
      setJevClear(false);
      const failed = [
        result.health.llm.healthy === false
          ? `${t("cockpit.llm")}: ${result.health.llm.error ?? "unhealthy"}`
          : null,
        result.health.jev.healthy === false
          ? `${t("cockpit.jev")}: ${result.health.jev.error ?? "unhealthy"}`
          : null,
      ].filter(Boolean);
      if (failed.length) setWarning(t("cockpit.healthFailed", { error: failed.join(" · ") }));
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal">
        <div className="eyebrow">{t("cockpit.models")}</div>
        <h2>{t("modelsModal.title")}</h2>
        <p className="page-lede">{t("modelsModal.lede")}</p>

        <hr className="divider" />

        <section className="stack">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div className="eyebrow">{t("modelsModal.llmSection")}</div>
            <Pill tone={models?.llm.configured ? "olive" : "yellow"}>
              {models?.llm.configured ? t("cockpit.configured") : t("cockpit.notConfigured")}
            </Pill>
          </div>
          <div className="grid grid-2">
            <Field label={t("modelsModal.baseUrl")}>
              <input
                className="input"
                value={llmBaseUrl}
                onChange={(event) => setLlmBaseUrl(event.target.value)}
                placeholder="https://…"
                autoComplete="off"
              />
            </Field>
            <Field label={t("modelsModal.model")}>
              <input
                className="input"
                value={llmModel}
                onChange={(event) => setLlmModel(event.target.value)}
                autoComplete="off"
              />
            </Field>
            <Field label={t("modelsModal.apiKey")} hint={t("modelsModal.apiKeyHint")}>
              <input
                className="input"
                type="password"
                value={llmKey}
                onChange={(event) => setLlmKey(event.target.value)}
                autoComplete="new-password"
              />
            </Field>
            <Field label={t("modelsModal.protocol")}>
              <select
                className="input"
                value={llmProtocol}
                onChange={(event) => setLlmProtocol(event.target.value)}
              >
                <option value="openai">openai</option>
                <option value="anthropic">anthropic</option>
              </select>
            </Field>
          </div>
          <label className="check">
            <input
              type="checkbox"
              checked={llmClear}
              onChange={(event) => setLlmClear(event.target.checked)}
            />
            {t("modelsModal.clearKey")}
          </label>
        </section>

        <hr className="divider" />

        <section className="stack">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div className="eyebrow">{t("modelsModal.jevSection")}</div>
            <Pill tone={models?.jev.configured ? "olive" : "yellow"}>
              {models?.jev.configured ? t("cockpit.configured") : t("cockpit.notConfigured")}
            </Pill>
          </div>
          <div className="grid grid-2">
            <Field label={t("modelsModal.baseUrl")}>
              <input
                className="input"
                value={jevBaseUrl}
                onChange={(event) => setJevBaseUrl(event.target.value)}
                placeholder="https://…"
                autoComplete="off"
              />
            </Field>
            <Field label={t("modelsModal.model")}>
              <input
                className="input"
                value={jevModel}
                onChange={(event) => setJevModel(event.target.value)}
                autoComplete="off"
              />
            </Field>
            <Field label={t("modelsModal.apiKey")} hint={t("modelsModal.apiKeyHint")}>
              <input
                className="input"
                type="password"
                value={jevKey}
                onChange={(event) => setJevKey(event.target.value)}
                autoComplete="new-password"
              />
            </Field>
            <Field label={t("modelsModal.path")}>
              <input
                className="input"
                value={jevPath}
                onChange={(event) => setJevPath(event.target.value)}
                autoComplete="off"
              />
            </Field>
          </div>
          <label className="check">
            <input
              type="checkbox"
              checked={jevClear}
              onChange={(event) => setJevClear(event.target.checked)}
            />
            {t("modelsModal.clearKey")}
          </label>
        </section>

        {error && <ErrorNote error={error} />}
        {notice && <div className="callout olive">{notice}</div>}
        {warning && <div className="callout danger">{warning}</div>}

        <div className="modal-actions">
          <button className="button ghost" onClick={onClose} disabled={busy}>
            {t("modelsModal.cancel")}
          </button>
          <button className="button" onClick={() => void save()} disabled={busy}>
            {busy ? t("modelsModal.saving") : t("modelsModal.save")}
          </button>
        </div>
      </div>
    </div>
  );
}
