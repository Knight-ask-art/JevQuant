import { useState } from "react";

import { EquityChart } from "../components/charts";
import { EmptyState, ErrorNote, Field, PaperCard, Pill, ScribbleUnderline } from "../components/ui";
import { useApi } from "../hooks/useApi";
import { useI18n } from "../i18n";
import { ApiError, api } from "../lib/api";
import { clock, label, money, pct, signed } from "../lib/format";
import type { BacktestReport, BacktestRun, SystemStatus } from "../lib/types";

const DEFAULT_SYMBOLS = ["BTCUSDT"];

export function Backtests() {
  const { t } = useI18n();
  const status = useApi<SystemStatus>("/api/v1/system/status", 20000);
  const runs = useApi<{ items: BacktestRun[] }>("/api/v1/backtests?limit=30", 15000);
  const [symbols, setSymbols] = useState<string[]>(DEFAULT_SYMBOLS);
  const [minutes, setMinutes] = useState(5);
  const [candles, setCandles] = useState(1500);
  const [seed, setSeed] = useState(42);
  const [stopBps, setStopBps] = useState("120");
  const [takeBps, setTakeBps] = useState("240");
  const [feeBps, setFeeBps] = useState("10");
  const [slippageBps, setSlippageBps] = useState("2");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [selected, setSelected] = useState<BacktestRun | null>(null);

  const universe = status.data?.server_limits.allowed_symbols ?? DEFAULT_SYMBOLS;

  function toggle(symbol: string) {
    setSymbols((current) =>
      current.includes(symbol)
        ? current.filter((item) => item !== symbol)
        : [...current, symbol],
    );
  }

  async function runBacktest() {
    setBusy(true);
    setError(null);
    try {
      const run = await api.post<BacktestRun>("/api/v1/backtests", {
        symbols,
        minutes,
        candles,
        seed,
        stop_loss_bps: stopBps,
        take_profit_bps: takeBps,
        fee_bps: feeBps,
        slippage_bps: slippageBps,
      });
      setSelected(run);
      await runs.reload();
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setBusy(false);
    }
  }

  const report = selected?.result as BacktestReport | undefined;
  const hasReport = Boolean(report && "summary" in report);

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="page-head">
        <div>
          <div className="page-kicker">{t("backtest.kicker")}</div>
          <h1>{t("backtest.title")}</h1>
          <ScribbleUnderline width={400} />
          <p className="page-lede">{t("backtest.lede")}</p>
        </div>
        <Pill tone="terracotta">{t("backtest.syntheticCandles")}</Pill>
      </div>

      {error && <ErrorNote error={error} />}

      <div className="two-col">
        <div className="stack">
          <PaperCard eyebrow={t("backtest.parameters")} title={t("backtest.newReplay")}>
            <div className="stack">
              <div className="field">
                <span>{t("backtest.universe")}</span>
                <div className="row">
                  {universe.map((symbol) => (
                    <button
                      key={symbol}
                      className={`button small ${symbols.includes(symbol) ? "" : "ghost"}`}
                      onClick={() => toggle(symbol)}
                    >
                      {symbol}
                    </button>
                  ))}
                </div>
              </div>
              <div className="grid grid-3">
                <Field label={t("backtest.barMinutes")}>
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={1440}
                    value={minutes}
                    onChange={(event) => setMinutes(Number(event.target.value))}
                  />
                </Field>
                <Field label={t("backtest.candlesPerSymbol")}>
                  <input
                    className="input"
                    type="number"
                    min={100}
                    max={20000}
                    value={candles}
                    onChange={(event) => setCandles(Number(event.target.value))}
                  />
                </Field>
                <Field label={t("backtest.seed")}>
                  <input
                    className="input"
                    type="number"
                    value={seed}
                    onChange={(event) => setSeed(Number(event.target.value))}
                  />
                </Field>
                <Field label={t("backtest.stopLossBps")}>
                  <input
                    className="input"
                    value={stopBps}
                    onChange={(event) => setStopBps(event.target.value)}
                  />
                </Field>
                <Field label={t("backtest.takeProfitBps")}>
                  <input
                    className="input"
                    value={takeBps}
                    onChange={(event) => setTakeBps(event.target.value)}
                  />
                </Field>
                <Field label={t("backtest.feeBps")}>
                  <input
                    className="input"
                    value={feeBps}
                    onChange={(event) => setFeeBps(event.target.value)}
                  />
                </Field>
                <Field label={t("backtest.slippageBps")}>
                  <input
                    className="input"
                    value={slippageBps}
                    onChange={(event) => setSlippageBps(event.target.value)}
                  />
                </Field>
              </div>
              <div className="row" style={{ justifyContent: "flex-end" }}>
                <button
                  className="button"
                  onClick={() => void runBacktest()}
                  disabled={busy || symbols.length === 0}
                >
                  {busy ? t("backtest.replaying") : t("backtest.runReplay")}
                </button>
              </div>
            </div>
          </PaperCard>

          <PaperCard eyebrow={t("backtest.history")} title={t("backtest.runs")}>
            {runs.data?.items?.length ? (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>{t("backtest.run")}</th>
                      <th>{t("common.status")}</th>
                      <th>{t("backtest.symbols")}</th>
                      <th className="num">{t("backtest.trades")}</th>
                      <th className="num">{t("backtest.netPnl")}</th>
                      <th>{t("common.created")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.data.items.map((run) => {
                      const result = run.result as Partial<BacktestReport>;
                      return (
                        <tr
                          key={run.id}
                          className="clickable"
                          onClick={() => setSelected(run)}
                          style={{
                            background:
                              selected?.id === run.id ? "var(--paper-2)" : undefined,
                          }}
                        >
                          <td className="mono">{run.id.slice(0, 7)}</td>
                          <td>
                            <Pill tone={run.status === "COMPLETED" ? "olive" : "yellow"}>
                              {label(run.status)}
                            </Pill>
                          </td>
                          <td className="mono small">
                            {((run.request.spec as { symbols?: string[] })?.symbols ?? []).join(
                              ", ",
                            )}
                          </td>
                          <td className="num">{result.summary?.trades ?? "—"}</td>
                          <td className="num">
                            {result.summary ? signed(result.summary.net_pnl_usdt) : "—"}
                          </td>
                          <td className="muted">{clock(run.created_at)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title={t("backtest.noRuns")} hint={t("backtest.noRunsHint")} />
            )}
          </PaperCard>
        </div>

        <div className="stack">
          {!selected ? (
            <PaperCard>
              <EmptyState
                title={t("backtest.noRunSelected")}
                hint={t("backtest.noRunSelectedHint")}
              />
            </PaperCard>
          ) : !hasReport ? (
            <PaperCard>
              <div className="callout">
                {t("backtest.noReport", {
                  status: label(selected.status),
                  code: selected.error_code ? ` · ${selected.error_code}` : "",
                })}
              </div>
            </PaperCard>
          ) : (
            <>
              <PaperCard
                eyebrow={t("backtest.summary")}
                title={
                  report!.summary.trades === 1
                    ? t("common.oneTrade")
                    : t("common.tradesCount", { n: report!.summary.trades })
                }
              >
                <div className="grid grid-2">
                  <div>
                    <div className="stat-label">{t("backtest.netPnl")}</div>
                    <div className="stat-value">{signed(report!.summary.net_pnl_usdt)}</div>
                  </div>
                  <div>
                    <div className="stat-label">{t("backtest.winRate")}</div>
                    <div className="stat-value">{pct(report!.summary.win_rate_pct, 1)}</div>
                  </div>
                  <div>
                    <div className="stat-label">{t("backtest.maxDrawdown")}</div>
                    <div className="stat-value">{pct(report!.summary.max_drawdown_pct, 2)}</div>
                  </div>
                  <div>
                    <div className="stat-label">{t("backtest.winsLosses")}</div>
                    <div className="stat-value">
                      {report!.summary.wins} / {report!.summary.losses}
                    </div>
                  </div>
                </div>
                <div className="row" style={{ marginTop: 12 }}>
                  <Pill tone="olive">{report!.engine}</Pill>
                  <Pill tone="terracotta">{t("backtest.synthetic")}</Pill>
                </div>
              </PaperCard>

              {Object.entries(report!.per_symbol).map(([symbol, result]) => (
                <PaperCard
                  key={symbol}
                  eyebrow={symbol}
                  title={`${signed(result.net_pnl_usdt)} USDT`}
                >
                  <EquityChart points={result.equity_curve} height={170} />
                  <div className="row" style={{ marginTop: 8 }}>
                    <span className="small muted">
                      {result.trades === 1
                        ? t("common.oneTrade")
                        : t("common.tradesCount", { n: result.trades })}
                    </span>
                    <span className="small muted">
                      {t("backtest.win", { value: pct(result.win_rate_pct, 1) })}
                    </span>
                    <span className="small muted">
                      {t("backtest.maxDD", { value: pct(result.max_drawdown_pct, 2) })}
                    </span>
                    <span className="small muted">
                      {t("backtest.ending", {
                        value: money(result.ending_equity_usdt),
                      })}
                    </span>
                  </div>
                </PaperCard>
              ))}

              <PaperCard eyebrow={t("backtest.honesty")} title={t("backtest.assumptions")}>
                <ol className="small" style={{ margin: 0, paddingLeft: 18 }}>
                  {report!.assumptions.map((assumption, index) => (
                    <li key={index} style={{ marginBottom: 6 }}>
                      {assumption}
                    </li>
                  ))}
                </ol>
              </PaperCard>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
