import { useState } from "react";

import { EmptyState, ErrorNote, Field, PaperCard, Pill, ScribbleUnderline } from "../components/ui";
import { useApi } from "../hooks/useApi";
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
    setSaved(key.trim() ? "API key stored in this browser only." : "API key cleared.");
  }

  const rows = (audit.data?.items ?? []).filter((event) =>
    typeFilter ? event.event_type.includes(typeFilter) : true,
  );

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="page-head">
        <div>
          <div className="page-kicker">Audit &amp; Settings</div>
          <h1>The paper trail</h1>
          <ScribbleUnderline width={250} />
          <p className="page-lede">
            Every state change is appended to a hash-linked chain. Credentials are never
            logged or returned; the API exposes only a redacted configuration summary.
          </p>
        </div>
        <div className="row">
          <button className="button" onClick={() => void runVerify()} disabled={busy}>
            {busy ? "Walking chain…" : "Verify audit chain"}
          </button>
        </div>
      </div>

      {error && <ErrorNote error={error} />}

      {verify && (
        <div className={`callout ${verify.valid ? "olive" : "danger"}`}>
          {verify.valid ? (
            <>
              <strong>Chain valid.</strong> {verify.count} events · head{" "}
              <span className="mono">{shortId(verify.head, 22)}</span>
            </>
          ) : (
            <>
              <strong>Chain broken.</strong> First bad sequence{" "}
              <span className="mono">{verify.broken_seq}</span> after {verify.count} events.
              Treat this as an incident: stop new entries and investigate.
            </>
          )}
        </div>
      )}

      <div className="two-col">
        <PaperCard
          eyebrow="Append-only"
          title={`${rows.length} audit events`}
          actions={
            <input
              className="input"
              style={{ width: 200 }}
              placeholder="filter event type"
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
                    <th>Seq</th>
                    <th>Event</th>
                    <th>Entity</th>
                    <th>Hash</th>
                    <th>At</th>
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
                            {event.entity_type} · {shortId(event.entity_id)}
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
            <EmptyState title="No audit events." hint="Start a cycle or change a mode." />
          )}
        </PaperCard>

        <div className="stack">
          <PaperCard eyebrow="Operator identity" title="API key">
            <p className="small" style={{ marginBottom: 10, color: "var(--ink-soft)" }}>
              When auth is enabled, the control plane requires{" "}
              <span className="mono">X-API-Key</span>. The key stays in this browser's local
              storage and is sent as a header; it is never persisted server-side by this UI.
              WebSocket auth uses a query parameter because browsers cannot set WS headers.
            </p>
            <Field label="Operator API key" hint="Leave empty for local auth-disabled mode">
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
                Save key
              </button>
              <button
                className="button ghost small"
                onClick={() => {
                  setKey("");
                  setApiKey("");
                  setSaved("API key cleared.");
                }}
              >
                Clear
              </button>
            </div>
            {saved && <p className="hand" style={{ marginTop: 8 }}>{saved}</p>}
          </PaperCard>

          <PaperCard eyebrow="Redacted config" title="Runtime settings">
            <div className="kv">
              {Object.entries(config.data?.config ?? {}).map(([name, value]) => (
                <FragmentRow key={name} name={name} value={value} />
              ))}
            </div>
          </PaperCard>

          <PaperCard eyebrow="Guarantees" title="What this page can promise">
            <div className="note-list">
              <span className="ok">Secrets are redacted before audit persistence.</span>
              <span className="ok">The audit chain is re-walked on demand.</span>
              <span className="ok">Tampering breaks verification at the touched sequence.</span>
              <span className="warn">
                A valid chain is integrity evidence, not proof of correct trading decisions.
              </span>
            </div>
            <div className="callout" style={{ marginTop: 12 }}>
              <Pill tone="yellow">read-only</Pill> This page does not mutate trading state
              except by storing the operator key in your browser.
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
