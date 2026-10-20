import { useEffect, useMemo, useState } from "react";

import { useI18n } from "../i18n";
import { ApiError, api } from "../lib/api";
import { compactMoney, pct } from "../lib/format";
import type { UniverseView } from "../lib/types";
import { ErrorNote } from "./ui";

export function SymbolPicker({
  open,
  universe,
  onClose,
  onSaved,
}: {
  open: boolean;
  universe: UniverseView | undefined;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useI18n();
  const [selected, setSelected] = useState<string[]>([]);
  const [manual, setManual] = useState("");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [rejected, setRejected] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!open) return;
    setSelected(universe?.symbols ?? []);
    setManual("");
    setQuery("");
    setError(null);
    setRejected({});
  }, [open, universe]);

  const rows = useMemo(() => {
    const candidates = universe?.candidates ?? [];
    const known = new Set(candidates.map((item) => item.symbol));
    // Symbols already selected but not in the venue listing (or when the
    // listing is unavailable) still need a row so they can be removed.
    const extras = selected
      .filter((symbol) => !known.has(symbol))
      .map((symbol) => ({
        symbol,
        base: symbol.replace(/USDT$/, ""),
        quote_volume_usdt: null,
        price_change_pct: null,
        last_price: null,
      }));
    const all = [...candidates, ...extras];
    const needle = query.trim().toUpperCase();
    return needle ? all.filter((item) => item.symbol.includes(needle)) : all;
  }, [universe, selected, query]);

  if (!open) return null;

  function toggle(symbol: string) {
    setSelected((current) =>
      current.includes(symbol) ? current.filter((item) => item !== symbol) : [...current, symbol],
    );
  }

  function addManual() {
    const symbol = manual.trim().toUpperCase();
    if (!/^[A-Z0-9]{3,16}USDT$/.test(symbol)) {
      setError(new ApiError(422, "VALIDATION_ERROR", t("symbols.manualHint")));
      return;
    }
    setError(null);
    if (!selected.includes(symbol)) setSelected((current) => [...current, symbol]);
    setManual("");
  }

  async function save() {
    if (!selected.length) {
      setError(new ApiError(422, "VALIDATION_ERROR", t("symbols.empty")));
      return;
    }
    setBusy(true);
    setError(null);
    setRejected({});
    try {
      const result = await api.put<{ symbols: string[]; rejected: Record<string, string> }>(
        "/api/v1/market/universe",
        { symbols: selected },
      );
      setSelected(result.symbols);
      if (Object.keys(result.rejected).length) {
        setRejected(result.rejected);
      } else {
        onSaved();
        onClose();
      }
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal">
        <div className="eyebrow">{t("cockpit.symbols")}</div>
        <h2>{t("symbols.title")}</h2>
        <p className="page-lede">{t("symbols.lede")}</p>

        <div className="row" style={{ marginBottom: 10 }}>
          <span className="eyebrow">{t("symbols.selected")}</span>
          <div className="row" style={{ flexWrap: "wrap" }}>
            {selected.length ? (
              selected.map((symbol) => (
                <button
                  key={symbol}
                  type="button"
                  className="chip active"
                  onClick={() => toggle(symbol)}
                  title={symbol}
                >
                  {symbol.replace(/USDT$/, "")} ✕
                </button>
              ))
            ) : (
              <span className="muted small">—</span>
            )}
          </div>
        </div>

        <div className="row" style={{ marginBottom: 10 }}>
          <input
            className="input"
            style={{ maxWidth: 180 }}
            placeholder={t("symbols.manualHint")}
            value={manual}
            onChange={(event) => setManual(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") addManual();
            }}
            autoComplete="off"
          />
          <button type="button" className="button ghost small" onClick={addManual}>
            {t("symbols.add")}
          </button>
          <input
            className="input"
            style={{ maxWidth: 180, marginLeft: "auto" }}
            placeholder="BTC, ETH…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            autoComplete="off"
          />
        </div>

        <div className="eyebrow" style={{ marginBottom: 6 }}>
          {t("symbols.available")}
        </div>
        {universe?.candidates?.length ? (
          <div className="symbol-grid">
            {rows.map((item) => {
              const active = selected.includes(item.symbol);
              const change = Number(item.price_change_pct ?? 0);
              return (
                <button
                  key={item.symbol}
                  type="button"
                  className={`chip${active ? " active" : ""}`}
                  onClick={() => toggle(item.symbol)}
                >
                  <span>{item.base}</span>
                  <span className={`small ${change < 0 ? "neg" : "pos"}`}>
                    {item.price_change_pct === null ? "" : pct(change, 1)}
                  </span>
                  <span className="small muted">
                    {item.quote_volume_usdt === null
                      ? ""
                      : compactMoney(item.quote_volume_usdt)}
                  </span>
                </button>
              );
            })}
          </div>
        ) : (
          <p className="hand">{t("symbols.noCandidates")}</p>
        )}

        {Object.keys(rejected).length > 0 && (
          <div className="callout danger" style={{ marginTop: 12 }}>
            {Object.entries(rejected).map(([symbol, reason]) => (
              <div key={symbol}>
                <span className="mono">{symbol}</span> — {reason}
              </div>
            ))}
          </div>
        )}
        {error && <ErrorNote error={error} />}

        <div className="modal-actions">
          <button className="button ghost" onClick={onClose} disabled={busy}>
            {t("symbols.cancel")}
          </button>
          <button className="button" onClick={() => void save()} disabled={busy}>
            {busy ? t("symbols.saving") : t("symbols.save")}
          </button>
        </div>
      </div>
    </div>
  );
}
