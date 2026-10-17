import { useState } from "react";

import { EquityChart } from "../components/charts";
import { EmptyState, ErrorNote, Field, PaperCard, Pill, ScribbleUnderline } from "../components/ui";
import { useApi } from "../hooks/useApi";
import { ApiError, api } from "../lib/api";
import { clock, money, pct, signed } from "../lib/format";
import type { BacktestReport, BacktestRun, SystemStatus } from "../lib/types";

const DEFAULT_SYMBOLS = ["BTCUSDT"];

export function Backtests() {
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
          <div className="page-kicker">Backtests</div>
          <h1>Replay, with the assumptions in ink</h1>
          <ScribbleUnderline width={400} />
          <p className="page-lede">
            This engine replays a fixed, disclosed rule set over generated candles. It is
            plumbing evidence — fees, sizing, stops, drawdown — not a performance claim. Real
            OHLCV/Freqtrade replay plugs into the same run record.
          </p>
        </div>
        <Pill tone="terracotta">synthetic candles</Pill>
      </div>

      {error && <ErrorNote error={error} />}

      <div className="two-col">
        <div className="stack">
          <PaperCard eyebrow="Parameters" title="New replay">
            <div className="stack">
              <div className="field">
                <span>Universe</span>
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
                <Field label="Bar minutes">
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={1440}
                    value={minutes}
                    onChange={(event) => setMinutes(Number(event.target.value))}
                  />
                </Field>
                <Field label="Candles / symbol">
                  <input
                    className="input"
                    type="number"
                    min={100}
                    max={20000}
                    value={candles}
                    onChange={(event) => setCandles(Number(event.target.value))}
                  />
                </Field>
                <Field label="Seed">
                  <input
                    className="input"
                    type="number"
                    value={seed}
                    onChange={(event) => setSeed(Number(event.target.value))}
                  />
                </Field>
                <Field label="Stop loss (bps)">
                  <input
                    className="input"
                    value={stopBps}
                    onChange={(event) => setStopBps(event.target.value)}
                  />
                </Field>
                <Field label="Take profit (bps)">
                  <input
                    className="input"
                    value={takeBps}
                    onChange={(event) => setTakeBps(event.target.value)}
                  />
                </Field>
                <Field label="Fee (bps)">
                  <input
                    className="input"
                    value={feeBps}
                    onChange={(event) => setFeeBps(event.target.value)}
                  />
                </Field>
                <Field label="Slippage (bps)">
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
                  {busy ? "Replaying…" : "Run replay"}
                </button>
              </div>
            </div>
          </PaperCard>

          <PaperCard eyebrow="History" title="Runs">
            {runs.data?.items?.length ? (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Run</th>
                      <th>Status</th>
                      <th>Symbols</th>
                      <th className="num">Trades</th>
                      <th className="num">Net PnL</th>
                      <th>Created</th>
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
                              {run.status}
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
              <EmptyState title="No runs yet." hint="Run a replay to create the first record." />
            )}
          </PaperCard>
        </div>

        <div className="stack">
          {!selected ? (
            <PaperCard>
              <EmptyState
                title="No run selected"
                hint="Summary, equity curves and the assumption list appear here."
              />
            </PaperCard>
          ) : !hasReport ? (
            <PaperCard>
              <div className="callout">
                This run has no report ({selected.status}
                {selected.error_code ? ` · ${selected.error_code}` : ""}).
              </div>
            </PaperCard>
          ) : (
            <>
              <PaperCard eyebrow="Summary" title={`${report!.summary.trades} trades`}>
                <div className="grid grid-2">
                  <div>
                    <div className="stat-label">Net PnL</div>
                    <div className="stat-value">{signed(report!.summary.net_pnl_usdt)}</div>
                  </div>
                  <div>
                    <div className="stat-label">Win rate</div>
                    <div className="stat-value">{pct(report!.summary.win_rate_pct, 1)}</div>
                  </div>
                  <div>
                    <div className="stat-label">Max drawdown</div>
                    <div className="stat-value">{pct(report!.summary.max_drawdown_pct, 2)}</div>
                  </div>
                  <div>
                    <div className="stat-label">Wins / losses</div>
                    <div className="stat-value">
                      {report!.summary.wins} / {report!.summary.losses}
                    </div>
                  </div>
                </div>
                <div className="row" style={{ marginTop: 12 }}>
                  <Pill tone="olive">{report!.engine}</Pill>
                  <Pill tone="terracotta">synthetic</Pill>
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
                    <span className="small muted">{result.trades} trades</span>
                    <span className="small muted">
                      win {pct(result.win_rate_pct, 1)}
                    </span>
                    <span className="small muted">
                      max DD {pct(result.max_drawdown_pct, 2)}
                    </span>
                    <span className="small muted">ending {money(result.ending_equity_usdt)}</span>
                  </div>
                </PaperCard>
              ))}

              <PaperCard eyebrow="Honesty" title="Assumptions">
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
