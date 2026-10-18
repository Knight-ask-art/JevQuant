import { useState } from "react";
import { useOutletContext } from "react-router-dom";

import { EquityChart } from "../components/charts";
import { EmptyState, ErrorNote, PaperCard, Pill, ScribbleUnderline } from "../components/ui";
import { useApi } from "../hooks/useApi";
import { useI18n } from "../i18n";
import { clock, label, money, pct, shortId, signed, timeAgo } from "../lib/format";
import type { OrderView, PnlPoint, PositionView, SystemStatus } from "../lib/types";

type Tab = "positions" | "orders" | "fills" | "ledger";

interface FillView {
  id: string;
  order_id: string;
  qty: string;
  price: string;
  fee_quote: string;
  fee_asset: string;
  created_at: string;
}

function orderTone(status: string): "" | "olive" | "terracotta" | "danger" | "yellow" {
  if (["FILLED", "SETTLED"].includes(status)) return "olive";
  if (["CANCELED", "CANCELLED", "REJECTED", "EXPIRED"].includes(status)) return "danger";
  if (["PARTIALLY_FILLED"].includes(status)) return "yellow";
  if (["ACCEPTED", "NEW", "SHADOWED"].includes(status)) return "terracotta";
  return "";
}

export function OrdersPositions() {
  const { t } = useI18n();
  const { status } = useOutletContext<{ status?: SystemStatus }>();
  const [tab, setTab] = useState<Tab>("positions");
  const positions = useApi<{ items: PositionView[] }>("/api/v1/positions?limit=100", 10000);
  const orders = useApi<{ items: OrderView[] }>("/api/v1/orders?limit=100", 10000);
  const fills = useApi<{ items: FillView[] }>("/api/v1/fills?limit=100", 15000);
  const pnl = useApi<{
    items: PnlPoint[];
    account: {
      realized_pnl_usdt: string;
      daily_realized_pnl_usdt: string;
      starting_equity_usdt: string;
      peak_equity_usdt: string;
    } | null;
  }>("/api/v1/pnl?limit=300", 15000);

  const points = (pnl.data?.items ?? [])
    .slice()
    .reverse()
    .map((point) => ({ at: point.as_of, equity_usdt: point.equity_usdt }));
  const openPositions = positions.data?.items ?? [];
  const orderCount = orders.data?.items?.length ?? 0;

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="page-head">
        <div>
          <div className="page-kicker">{t("orders.kicker")}</div>
          <h1>{t("orders.title")}</h1>
          <ScribbleUnderline width={270} />
          <p className="page-lede">{t("orders.lede")}</p>
        </div>
        <div className="row">
          <Pill tone="olive">
            {t("orders.openCount", { n: openPositions.length })}
          </Pill>
          <Pill>
            {orderCount === 1
              ? t("common.oneOrder")
              : t("orders.ordersCount", { n: orderCount })}
          </Pill>
        </div>
      </div>

      <div className="grid grid-3">
        <PaperCard>
          <div className="stat-label">{t("orders.realizedPnl")}</div>
          <div className="stat-value">{signed(pnl.data?.account?.realized_pnl_usdt)}</div>
          <div className="stat-note">
            {t("orders.sinceInception", {
              mode: label(status?.mode?.mode ?? "paper"),
            })}
          </div>
        </PaperCard>
        <PaperCard>
          <div className="stat-label">{t("orders.dailyRealized")}</div>
          <div className="stat-value">{signed(pnl.data?.account?.daily_realized_pnl_usdt)}</div>
          <div className="stat-note">{t("orders.resetsUtc")}</div>
        </PaperCard>
        <PaperCard>
          <div className="stat-label">{t("orders.peakEquity")}</div>
          <div className="stat-value">{money(pnl.data?.account?.peak_equity_usdt)}</div>
          <div className="stat-note">
            {t("orders.startedAt", {
              value: money(pnl.data?.account?.starting_equity_usdt),
            })}
          </div>
        </PaperCard>
      </div>

      <div className="tabs">
        {(
          [
            ["positions", t("orders.tabPositions")],
            ["orders", t("orders.tabOrders")],
            ["fills", t("orders.tabFills")],
            ["ledger", t("orders.tabLedger")],
          ] as Array<[Tab, string]>
        ).map(([value, text]) => (
          <button
            key={value}
            className={tab === value ? "active" : ""}
            onClick={() => setTab(value)}
          >
            {text}
          </button>
        ))}
      </div>

      {tab === "positions" && (
        <PaperCard eyebrow={t("orders.spotLongOnly")} title={t("orders.openPositions")}>
          <ErrorNote error={positions.error} />
          {openPositions.length ? (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>{t("common.symbol")}</th>
                    <th className="num">{t("common.qty")}</th>
                    <th className="num">{t("common.entry")}</th>
                    <th className="num">{t("common.mark")}</th>
                    <th className="num">{t("common.unrealized")}</th>
                    <th className="num">{t("orders.stop")}</th>
                    <th className="num">{t("orders.take")}</th>
                    <th>{t("common.opened")}</th>
                    <th>{t("common.mode")}</th>
                  </tr>
                </thead>
                <tbody>
                  {openPositions.map((position) => (
                    <tr key={position.id}>
                      <td>
                        <strong>{position.symbol}</strong>
                      </td>
                      <td className="num">{position.qty}</td>
                      <td className="num">{money(position.avg_entry_price)}</td>
                      <td className="num">{money(position.mark_price)}</td>
                      <td className="num">{signed(position.unrealized_pnl_usdt)}</td>
                      <td className="num mono">{position.stop_loss_bps} bps</td>
                      <td className="num mono">{position.take_profit_bps} bps</td>
                      <td className="muted">{timeAgo(position.opened_at)}</td>
                      <td>
                        <Pill tone={position.is_simulated ? "terracotta" : "olive"}>
                          {position.is_simulated ? t("common.simulated") : t("common.live")}
                        </Pill>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title={t("orders.flat")} hint={t("orders.flatHint")} />
          )}
        </PaperCard>
      )}

      {tab === "orders" && (
        <PaperCard
          eyebrow={t("orders.executionStateMachine")}
          title={t("orders.tabOrders")}
        >
          <ErrorNote error={orders.error} />
          {orders.data?.items?.length ? (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>{t("orders.order")}</th>
                    <th>{t("common.symbol")}</th>
                    <th>{t("common.side")}</th>
                    <th>{t("common.type")}</th>
                    <th>{t("common.status")}</th>
                    <th className="num">{t("common.qty")}</th>
                    <th className="num">{t("orders.fill")}</th>
                    <th className="num">{t("orders.avgPrice")}</th>
                    <th className="num">{t("signals.fee")}</th>
                    <th>{t("common.mode")}</th>
                    <th>{t("common.created")}</th>
                  </tr>
                </thead>
                <tbody>
                  {orders.data.items.map((order) => (
                    <tr key={order.id}>
                      <td className="mono">{shortId(order.id, 7)}</td>
                      <td>
                        <strong>{order.symbol}</strong>
                      </td>
                      <td>{label(order.side)}</td>
                      <td>{label(order.order_type)}</td>
                      <td>
                        <Pill tone={orderTone(order.status)}>{label(order.status)}</Pill>
                      </td>
                      <td className="num">{order.qty}</td>
                      <td className="num">{order.filled_qty}</td>
                      <td className="num">{money(order.avg_fill_price)}</td>
                      <td className="num">{money(order.fee_quote, 4)}</td>
                      <td>
                        <span className="mono small">{label(order.mode)}</span>
                      </td>
                      <td className="muted">{clock(order.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title={t("orders.noOrders")} hint={t("orders.noOrdersHint")} />
          )}
        </PaperCard>
      )}

      {tab === "fills" && (
        <PaperCard eyebrow={t("orders.appendOnly")} title={t("orders.fills")}>
          <ErrorNote error={fills.error} />
          {fills.data?.items?.length ? (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>{t("orders.fill")}</th>
                    <th>{t("orders.order")}</th>
                    <th className="num">{t("common.qty")}</th>
                    <th className="num">{t("common.price")}</th>
                    <th className="num">{t("signals.fee")}</th>
                    <th>{t("orders.at")}</th>
                  </tr>
                </thead>
                <tbody>
                  {fills.data.items.map((fill) => (
                    <tr key={fill.id}>
                      <td className="mono">{shortId(fill.id, 7)}</td>
                      <td className="mono">{shortId(fill.order_id, 7)}</td>
                      <td className="num">{fill.qty}</td>
                      <td className="num">{money(fill.price)}</td>
                      <td className="num">
                        {money(fill.fee_quote, 4)} {fill.fee_asset}
                      </td>
                      <td className="muted">{clock(fill.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title={t("orders.noFills")} hint={t("orders.noFillsHint")} />
          )}
        </PaperCard>
      )}

      {tab === "ledger" && (
        <div className="stack">
          <PaperCard eyebrow={t("orders.equity")} title={t("orders.snapshots")}>
            <ErrorNote error={pnl.error} />
            <EquityChart points={points} />
          </PaperCard>
          <PaperCard eyebrow={t("orders.snapshotRows")} title={t("orders.ledgerDetail")}>
            {pnl.data?.items?.length ? (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>{t("orders.asOf")}</th>
                      <th className="num">{t("orders.equity")}</th>
                      <th className="num">{t("orders.cash")}</th>
                      <th className="num">{t("orders.realized")}</th>
                      <th className="num">{t("common.unrealized")}</th>
                      <th className="num">{t("orders.gross")}</th>
                      <th className="num">{t("overview.drawdown")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pnl.data.items.slice(0, 60).map((point) => (
                      <tr key={point.id}>
                        <td>{clock(point.as_of)}</td>
                        <td className="num">{money(point.equity_usdt)}</td>
                        <td className="num">{money(point.cash_quote_usdt)}</td>
                        <td className="num">{signed(point.realized_pnl_usdt)}</td>
                        <td className="num">{signed(point.unrealized_pnl_usdt)}</td>
                        <td className="num">{money(point.gross_exposure_usdt)}</td>
                        <td className="num">{pct(point.drawdown_pct, 3)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title={t("orders.noLedger")} hint={t("orders.noLedgerHint")} />
            )}
          </PaperCard>
        </div>
      )}
    </div>
  );
}
