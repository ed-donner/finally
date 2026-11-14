"use client";

import { useAppData } from "./AppDataProvider";
import { livePositions } from "@/lib/portfolio";
import { usePrices } from "@/lib/priceStore";
import { formatMoney, formatPercent, formatPrice, formatQuantity, formatSignedMoney, toneClass } from "@/lib/format";

export function PositionsTable() {
  const { portfolio, select, selected } = useAppData();
  const prices = usePrices();
  const positions = livePositions(portfolio?.positions ?? [], prices).sort(
    (a, b) => b.market_value - a.market_value,
  );

  return (
    <section className="panel flex min-h-0 flex-col" aria-label="Positions">
      <div className="panel-head">
        <h2 className="panel-title">Positions</h2>
        <span className="text-xs text-dim num">{positions.length}</span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        <table data-testid="positions-table" className="data-table w-full">
          <thead>
            <tr>
              <th className="text-left">Symbol</th>
              <th>Qty</th>
              <th>Avg cost</th>
              <th>Last</th>
              <th>Mkt value</th>
              <th>Unrealized P&L</th>
              <th>P&L %</th>
              <th>Weight</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr
                key={p.ticker}
                data-testid={`position-row-${p.ticker}`}
                data-active={p.ticker === selected || undefined}
                onClick={() => select(p.ticker)}
                className="cursor-pointer"
              >
                <td className="text-left font-semibold text-text">{p.ticker}</td>
                <td>{formatQuantity(p.quantity)}</td>
                <td>{formatPrice(p.avg_cost)}</td>
                <td>{formatPrice(p.current_price)}</td>
                <td>{formatMoney(p.market_value)}</td>
                <td className={toneClass(p.unrealized_pnl)}>{formatSignedMoney(p.unrealized_pnl)}</td>
                <td className={toneClass(p.unrealized_pnl_percent)}>{formatPercent(p.unrealized_pnl_percent)}</td>
                <td className="text-muted">{(p.weight * 100).toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
        {positions.length === 0 && (
          <p className="px-3 py-5 text-center text-xs text-muted">
            No open positions. Buy shares with the trade bar or ask FinAlly to place an order.
          </p>
        )}
      </div>
    </section>
  );
}
