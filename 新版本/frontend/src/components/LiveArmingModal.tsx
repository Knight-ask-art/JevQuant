import { useEffect, useState } from "react";

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
  onArmed: () => void;
}) {
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
      onArmed();
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
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Arm live trading">
      <div className="modal">
        <div className="eyebrow">Live gate · not a toggle</div>
        <h2>Arm live trading</h2>
        <p className="page-lede">
          This is a server-side handshake. Preflight inspects the environment, Binance key
          restrictions, Hummingbot, feed, database and Redis. Then a single-use token must be
          redeemed with the confirmation phrase and an application TOTP code.
        </p>

        {armedAt ? (
          <div className="callout danger" style={{ marginTop: 18 }}>
            <strong>Live armed.</strong> Every page now carries the live ribbon. Use “Disarm”
            on Risk &amp; Modes to stop new entries immediately.
          </div>
        ) : (
          <>
            <hr className="divider" />
            <div className="eyebrow" style={{ marginBottom: 8 }}>
              Preflight {busy ? "running…" : ready ? "passed" : "result"}
            </div>
            {busy && !checks.length && <Loading what="preflight checks" />}
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
                {failed.length} precondition{failed.length === 1 ? "" : "s"} failed. Live
                arming is refused. Fix the environment; do not work around it.
              </div>
            )}

            {ready && (
              <>
                <hr className="divider" />
                <div className="row" style={{ marginBottom: 12 }}>
                  <Pill tone="olive">preflight passed</Pill>
                  <Pill>single-use token held in memory</Pill>
                </div>
                <div className="grid grid-2">
                  <Field
                    label="Confirmation phrase"
                    hint={`Type exactly: ${CONFIRMATION}`}
                  >
                    <input
                      className="input"
                      value={phrase}
                      onChange={(event) => setPhrase(event.target.value)}
                      autoComplete="off"
                      spellCheck={false}
                    />
                  </Field>
                  <Field label="Application TOTP" hint="6-digit code from your authenticator">
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
            {armedAt ? "Close" : "Cancel"}
          </button>
          {!armedAt && (
            <button
              className="button danger"
              onClick={() => void arm()}
              disabled={!ready || busy || phrase !== CONFIRMATION || totp.length < 6}
            >
              {busy ? "Working…" : "Arm live trading"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
