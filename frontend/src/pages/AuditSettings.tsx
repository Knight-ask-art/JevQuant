import { useState } from "react";

import { EmptyState, ErrorNote, Field, PaperCard, Pill, ScribbleUnderline } from "../components/ui";
import { useApi } from "../hooks/useApi";
import { useI18n } from "../i18n";
import { ApiError, api, setCsrfToken } from "../lib/api";
import { clock, label, shortId } from "../lib/format";
import type { AuditEvent } from "../lib/types";

interface VerifyResult {
  valid: boolean;
  count: number;
  head?: string;
  broken_seq?: number;
}

export function AuditSettings() {
  const { t, lang } = useI18n();
  const audit = useApi<{ items: AuditEvent[] }>("/api/v1/audit?limit=120", 15000);
  const config = useApi<{
    config: Record<string, unknown>;
    execution_backend: string;
    data_source: string;
  }>("/api/v1/system/config", 30000);
  const [verify, setVerify] = useState<VerifyResult | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const auth = useApi<{ enabled: boolean; authenticated: boolean; can_disable: boolean }>("/api/v1/auth/status", 0);
  const [key, setKey] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
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

  async function toggleAuthentication(enabled: boolean) {
    setAuthBusy(true);
    setError(null);
    try {
      if (enabled) {
        const login = await api.post<{ csrf_token: string }>("/api/v1/auth/login", { api_key: key });
        setCsrfToken(login.csrf_token);
      }
      await api.put("/api/v1/auth/settings", { enabled, ...(enabled ? { admin_key: key } : {}) });
      setKey("");
      window.location.reload();
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setAuthBusy(false);
    }
  }

  async function signOut() {
    await api.post("/api/v1/auth/logout");
    setCsrfToken(null);
    window.location.reload();
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
          <PaperCard eyebrow={t("audit.operatorIdentity")} title={lang === "zh" ? "网页访问认证" : "Web operator authentication"}>
            <p className="small" style={{ marginBottom: 10, color: "var(--ink-soft)" }}>
              {lang === "zh"
                ? "本机合成行情模拟盘可关闭认证；测试网、实盘和生产环境始终需要认证。密钥不保存在浏览器存储中。"
                : "Optional for local synthetic paper trading. Always required for testnet, live and production. Your key is never saved in browser storage."}
            </p>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <Pill tone={auth.data?.enabled ? "olive" : "yellow"}>
                {auth.data?.enabled ? (lang === "zh" ? "已开启" : "Enabled") : (lang === "zh" ? "已关闭" : "Disabled")}
              </Pill>
              <label className="row small">
                <input type="checkbox" aria-label="Enable operator authentication"
                  checked={auth.data?.enabled ?? false}
                  disabled={authBusy || !auth.data || (auth.data.enabled && !auth.data.can_disable)}
                  onChange={(event) => void toggleAuthentication(event.target.checked)} />
                {lang === "zh" ? "开启认证" : "Require login"}
              </label>
            </div>
            {!auth.data?.enabled && (
              <Field label={t("audit.operatorKey")} hint={lang === "zh" ? "开启认证前输入 .env 中配置的管理员密钥" : "Enter your configured administrator key before enabling"}>
                <input className="input" type="password" value={key} onChange={(event) => setKey(event.target.value)} autoComplete="new-password" />
              </Field>
            )}
            {auth.data?.enabled && <button className="button ghost small" style={{ marginTop: 12 }}
              onClick={() => void signOut()}>{lang === "zh" ? "退出登录" : "Sign out"}</button>}
            {!auth.data?.can_disable && <p className="hand" style={{ marginTop: 10 }}>
              {lang === "zh" ? "当前运行环境禁止关闭认证。" : "Authentication cannot be disabled in this environment."}
            </p>}
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
