import { useState } from "react";

import { EmptyState, ErrorNote, Field, PaperCard, Pill, ScribbleUnderline } from "../components/ui";
import { useApi } from "../hooks/useApi";
import { useI18n } from "../i18n";
import { ApiError, api, getApiKey, setApiKey } from "../lib/api";
import { clock, label, shortId } from "../lib/format";
import type { AuditEvent } from "../lib/types";

interface VerifyResult {
  valid: boolean;
  count: number;
  head?: string;
  broken_seq?: number;
}

export function AuditSettings() {
  const { t } = useI18n();
  const audit = useApi<{ items: AuditEvent[] }>("/api/v1/audit?limit=120", 15000);
  const config = useApi<{
    config: Record<string, unknown>;
    execution_backend: string;
    data_source: string;
  }>("/api/v1/system/config", 30000);
  const [verify, setVerify] = useState<VerifyResult | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [key, setKey] = useState(getApiKey());
  const [saved, setSaved] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState("");

  async function runVerify() {
    setBusy(true);
    setError(null);
    try {
      setVerify(await api.get<VerifyResult>("/api/v1/audit/verify"));
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setBusy(false);
    }
  }

  function saveKey() {
    setApiKey(key.trim());
    setSaved(key.trim() ? t("audit.keyStored") : t("audit.keyCleared"));
  }

  const rows = (audit.data?.items ?? []).filter((event) =>
    typeFilter ? event.event_type.includes(typeFilter) : true,
  );

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="page-head">
        <div>
          <div className="page-kicker">{t("audit.kicker")}</div>
          <h1>{t("audit.title")}</h1>
          <ScribbleUnderline width={250} />
          <p className="page-lede">{t("audit.lede")}</p>
        </div>
        <div className="row">
          <button className="button" onClick={() => void runVerify()} disabled={busy}>
            {busy ? t("audit.walking") : t("audit.verify")}
          </button>
        </div>
      </div>

      {error && <ErrorNote error={error} />}

      {verify && (
        <div className={`callout ${verify.valid ? "olive" : "danger"}`}>
          {verify.valid ? (
            <>
              <strong>{t("audit.chainValid")}</strong>{" "}
              {t("audit.chainValidBody", {
                count: verify.count,
                head: shortId(verify.head, 22),
              })}
            </>
          ) : (
            <>
              <strong>{t("audit.chainBroken")}</strong>{" "}
              {t("audit.chainBrokenBody", {
                seq: verify.broken_seq ?? "—",
                count: verify.count,
              })}
            </>
          )}
        </div>
      )}

      <div className="two-col">
        <PaperCard
          eyebrow={t("audit.appendOnly")}
          title={
            rows.length === 1
              ? t("common.oneAuditEvent")
              : t("common.auditEvents", { n: rows.length })
          }
          actions={
            <input
              className="input"
              style={{ width: 200 }}
              placeholder={t("audit.filterPlaceholder")}
              value={typeFilter}
              onChange={(event) => setTypeFilter(event.target.value)}
            />
          }
        >
          <ErrorNote error={audit.error} />
          {rows.length ? (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>{t("audit.seq")}</th>
                    <th>{t("audit.event")}</th>
                    <th>{t("audit.entity")}</th>
                    <th>{t("audit.hash")}</th>
                    <th>{t("audit.at")}</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((event) => (
                    <tr key={event.id}>
                      <td className="mono">{event.seq}</td>
                      <td>
                        <strong>{event.event_type}</strong>
                        {event.entity_id && (
                          <div className="muted small mono">
                            {label(event.entity_type)} · {shortId(event.entity_id)}
                          </div>
                        )}
                      </td>
                      <td className="mono small">{shortId(event.event_hash, 12)}</td>
                      <td className="mono small muted">{shortId(event.prev_hash, 12)}</td>
                      <td className="muted">{clock(event.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title={t("audit.noEvents")} hint={t("audit.noEventsHint")} />
          )}
        </PaperCard>

        <div className="stack">
          <PaperCard eyebrow={t("audit.operatorIdentity")} title={t("audit.apiKey")}>
            <p className="small" style={{ marginBottom: 10, color: "var(--ink-soft)" }}>
              {t("audit.apiKeyBody")}
            </p>
            <Field label={t("audit.operatorKey")} hint={t("audit.operatorKeyHint")}>
              <input
                className="input"
                type="password"
                value={key}
                onChange={(event) => setKey(event.target.value)}
                autoComplete="off"
              />
            </Field>
            <div className="row" style={{ marginTop: 10 }}>
              <button className="button small" onClick={saveKey}>
                {t("audit.saveKey")}
              </button>
              <button
                className="button ghost small"
                onClick={() => {
                  setKey("");
                  setApiKey("");
                  setSaved(t("audit.keyCleared"));
                }}
              >
                {t("audit.clear")}
              </button>
            </div>
            {saved && (
              <p className="hand" style={{ marginTop: 8 }}>
                {saved}
              </p>
            )}
          </PaperCard>

          <PaperCard eyebrow={t("audit.redactedConfig")} title={t("audit.runtimeSettings")}>
            <div className="kv">
              {Object.entries(config.data?.config ?? {}).map(([name, value]) => (
                <FragmentRow key={name} name={name} value={value} />
              ))}
            </div>
          </PaperCard>

          <PaperCard eyebrow={t("audit.guarantees")} title={t("audit.whatPromises")}>
            <div className="note-list">
              <span className="ok">{t("audit.secretsRedacted")}</span>
              <span className="ok">{t("audit.chainRewalked")}</span>
              <span className="ok">{t("audit.tampering")}</span>
              <span className="warn">{t("audit.integrityNotProof")}</span>
            </div>
            <div className="callout" style={{ marginTop: 12 }}>
              <Pill tone="yellow">{t("audit.readOnly")}</Pill> {t("audit.readOnlyBody")}
            </div>
          </PaperCard>
        </div>
      </div>
    </div>
  );
}

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
