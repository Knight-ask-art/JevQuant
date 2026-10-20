import { useEffect, useState } from "react";

import { useI18n } from "../i18n";
import { api } from "../lib/api";
import { Field, Loading, Pill } from "./ui";

interface Check {
  name: string;
  passed: boolean;
  detail: string;
}

const CONFIRMATION = "ARM LIVE TRADING";

export function LiveArmingModal({
  open,
  onClose,
  onArmed,
}: {
  open: boolean;
  onClose: () => void;
  onArmed: () => boolean | void;
}) {
  const { t } = useI18n();
  const [checks, setChecks] = useState<Check[]>([]);
  const [token, setToken] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [passing, setPassing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [phrase, setPhrase] = useState("");
  const [totp, setTotp] = useState("");
  const [armedAt, setArmedAt] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setChecks([]);
    setToken(null);
    setError(null);
    setPhrase("");
    setTotp("");
    setArmedAt(null);
    setBusy(true);
    setPassing(false);
    api
      .post<{ passed: boolean; checks: Check[]; arming_token: string | null }>(
        "/api/v1/mode/preflight-live",
      )
      .then((result) => {
        setChecks(result.checks);
        setToken(result.arming_token);
        setPassing(result.passed);
      })
      .catch((caught: unknown) => setError(String(caught)))
      .finally(() => setBusy(false));
  }, [open]);

  if (!open) return null;

  async function arm() {
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/v1/mode/arm-live", {
        arming_token: token,
        confirmation: phrase,
        totp_code: totp,
      });
      setArmedAt(new Date().toISOString());
      if (onArmed() !== false) onClose();
    } catch (caught) {
      // A token is single-use; any failure requires a fresh preflight.
      setError(String(caught));
      setToken(null);
      setPassing(false);
    } finally {
      setBusy(false);
    }
  }

  const failed = checks.filter((check) => !check.passed);
  const ready = passing && Boolean(token);

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label={t("arming.aria")}
    >
      <div className="modal">
        <div className="eyebrow">{t("arming.eyebrow")}</div>
        <h2>{t("arming.title")}</h2>
        <p className="page-lede">{t("arming.lede")}</p>

        {armedAt ? (
          <div className="callout danger" style={{ marginTop: 18 }}>
            <strong>{t("arming.armed")}</strong> {t("arming.armedBody")}
          </div>
        ) : (
          <>
            <hr className="divider" />
            <div className="eyebrow" style={{ marginBottom: 8 }}>
              {busy
                ? t("arming.preflightRunning")
                : ready
                  ? t("arming.preflightPassed")
                  : t("arming.preflightResult")}
            </div>
            {busy && !checks.length && <Loading what={t("common.preflightChecks")} />}
            {checks.length > 0 && (
              <div className="checklist">
                {checks.map((check) => (
                  <div className="check-row" key={check.name}>
                    <span style={{ color: check.passed ? "var(--olive)" : "var(--danger)" }}>
                      {check.passed ? "✓" : "✕"}
                    </span>
                    <span className="mono">{check.name}</span>
                    <span className="muted small">{check.detail}</span>
                  </div>
                ))}
              </div>
            )}

            {!busy && failed.length > 0 && (
              <div className="callout danger" style={{ marginTop: 14 }}>
                {failed.length === 1
                  ? t("arming.failedOne")
                  : t("arming.failedMany", { n: failed.length })}
              </div>
            )}

            {ready && (
              <>
                <hr className="divider" />
                <div className="row" style={{ marginBottom: 12 }}>
                  <Pill tone="olive">{t("arming.passedPill")}</Pill>
                  <Pill>{t("arming.tokenPill")}</Pill>
                </div>
                <div className="grid grid-2">
                  <Field
                    label={t("arming.confirmation")}
                    hint={t("arming.confirmationHint", { phrase: CONFIRMATION })}
                  >
                    <input
                      className="input"
                      value={phrase}
                      onChange={(event) => setPhrase(event.target.value)}
                      autoComplete="off"
                      spellCheck={false}
                    />
                  </Field>
                  <Field label={t("arming.totp")} hint={t("arming.totpHint")}>
                    <input
                      className="input"
                      value={totp}
                      onChange={(event) => setTotp(event.target.value.replace(/\D/g, ""))}
                      inputMode="numeric"
                      maxLength={8}
                      autoComplete="one-time-code"
                    />
                  </Field>
                </div>
              </>
            )}
          </>
        )}

        {error && (
          <div className="callout danger" style={{ marginTop: 14 }}>
            {error}
          </div>
        )}

        <div className="modal-actions">
          <button className="button ghost" onClick={onClose} disabled={busy}>
            {armedAt ? t("arming.close") : t("arming.cancel")}
          </button>
          {!armedAt && (
            <button
              className="button danger"
              onClick={() => void arm()}
              disabled={!ready || busy || phrase !== CONFIRMATION || totp.length < 6}
            >
              {busy ? t("arming.working") : t("arming.arm")}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
