import { useState } from "react";

import { EquityChart } from "../components/charts";
import { EmptyState, ErrorNote, PaperCard, Pill, ScribbleUnderline } from "../components/ui";
import { useApi } from "../hooks/useApi";
import { clock, money, pct, shortId, signed, timeAgo } from "../lib/format";
import type { OrderView, PnlPoint, PositionView } from "../lib/types";

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

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="page-head">
        <div>
          <div className="page-kicker">Orders &amp; Positions</div>
          <h1>The execution ledger</h1>
          <ScribbleUnderline width={270} />
          <p className="page-lede">
            Paper, Shadow, Testnet and Hummingbot consume the same frozen approved intent.
            Every fill, fee and position change is written once, with the mode and simulation
            flag that produced it.
          </p>
        </div>
        <div className="row">
          <Pill tone="olive">{openPositions.length} open</Pill>
          <Pill>{orders.data?.items?.length ?? 0} orders</Pill>
        </div>
      </div>

      <div className="grid grid-3">
        <PaperCard>
          <div className="stat-label">Realized PnL</div>
          <div className="stat-value">{signed(pnl.data?.account?.realized_pnl_usdt)}</div>
          <div className="stat-note">since inception · paper ledger</div>
        </PaperCard>
        <PaperCard>
          <div className="stat-label">Daily realized</div>
          <div className="stat-value">{signed(pnl.data?.account?.daily_realized_pnl_usdt)}</div>
          <div className="stat-note">resets on the UTC date boundary</div>
        </PaperCard>
        <PaperCard>
          <div className="stat-label">Peak equity</div>
          <div className="stat-value">{money(pnl.data?.account?.peak_equity_usdt)}</div>
          <div className="stat-note">
            started at {money(pnl.data?.account?.starting_equity_usdt)}
          </div>
        </PaperCard>
      </div>

      <div className="tabs">
        {(
          [
            ["positions", "Positions"],
            ["orders", "Orders"],
            ["fills", "Fills"],
            ["ledger", "PnL ledger"],
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
        <PaperCard eyebrow="Spot long-only" title="Open positions">
          <ErrorNote error={positions.error} />
          {openPositions.length ? (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th className="num">Qty</th>
                    <th className="num">Entry</th>
                    <th className="num">Mark</th>
                    <th className="num">Unrealized</th>
                    <th className="num">Stop</th>
                    <th className="num">Take</th>
                    <th>Opened</th>
                    <th>Mode</th>
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
                          {position.is_simulated ? "simulated" : "live"}
                        </Pill>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState
              title="Flat."
              hint="Positions appear after an approved entry fills. Stops and take-profits are deterministic."
            />
          )}
        </PaperCard>
      )}

      {tab === "orders" && (
        <PaperCard eyebrow="Execution state machine" title="Orders">
          <ErrorNote error={orders.error} />
          {orders.data?.items?.length ? (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Order</th>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th>Type</th>
                    <th>Status</th>
                    <th className="num">Qty</th>
                    <th className="num">Filled</th>
                    <th className="num">Avg price</th>
                    <th className="num">Fee</th>
                    <th>Mode</th>
                    <th>Created</th>
                  </tr>
                </thead>
                <tbody>
                  {orders.data.items.map((order) => (
                    <tr key={order.id}>
                      <td className="mono">{shortId(order.id, 7)}</td>
                      <td>
                        <strong>{order.symbol}</strong>
                      </td>
                      <td>{order.side}</td>
                      <td>{order.order_type}</td>
                      <td>
                        <Pill tone={orderTone(order.status)}>{order.status}</Pill>
                      </td>
                      <td className="num">{order.qty}</td>
                      <td className="num">{order.filled_qty}</td>
                      <td className="num">{money(order.avg_fill_price)}</td>
                      <td className="num">{money(order.fee_quote, 4)}</td>
                      <td>
                        <span className="mono small">{order.mode}</span>
                      </td>
                      <td className="muted">{clock(order.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="No orders yet." hint="Approved intents become orders here." />
          )}
        </PaperCard>
      )}

      {tab === "fills" && (
        <PaperCard eyebrow="Append-only" title="Fills">
          <ErrorNote error={fills.error} />
          {fills.data?.items?.length ? (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Fill</th>
                    <th>Order</th>
                    <th className="num">Qty</th>
                    <th className="num">Price</th>
                    <th className="num">Fee</th>
                    <th>At</th>
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
            <EmptyState title="No fills yet." hint="The paper adapter writes a fill per execution." />
          )}
        </PaperCard>
      )}

      {tab === "ledger" && (
        <div className="stack">
          <PaperCard eyebrow="Equity" title="Snapshots">
            <ErrorNote error={pnl.error} />
            <EquityChart points={points} />
          </PaperCard>
          <PaperCard eyebrow="Snapshot rows" title="Ledger detail">
            {pnl.data?.items?.length ? (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>As of</th>
                      <th className="num">Equity</th>
                      <th className="num">Cash</th>
                      <th className="num">Realized</th>
                      <th className="num">Unrealized</th>
                      <th className="num">Gross</th>
                      <th className="num">Drawdown</th>
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
              <EmptyState title="No ledger rows." hint="A cycle writes one snapshot." />
            )}
          </PaperCard>
        </div>
      )}
    </div>
  );
}
