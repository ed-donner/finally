"use client";

import { useStore } from "@/lib/store";
import { formatCurrency, formatPrice, formatPct, formatPnL } from "@/lib/format";

export function PositionsTable() {
  const portfolio = useStore((s) => s.portfolio);

  if (!portfolio?.positions.length) {
    return (
      <div className="flex items-center justify-center h-full text-text-secondary text-xs">
        No positions — make a trade to get started
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-text-secondary border-b border-border">
            <th className="text-left px-2 py-1.5 font-medium">Ticker</th>
            <th className="text-right px-2 py-1.5 font-medium">Qty</th>
            <th className="text-right px-2 py-1.5 font-medium">Avg Cost</th>
            <th className="text-right px-2 py-1.5 font-medium">Price</th>
            <th className="text-right px-2 py-1.5 font-medium">P&L</th>
            <th className="text-right px-2 py-1.5 font-medium">%</th>
          </tr>
        </thead>
        <tbody>
          {portfolio.positions.map((pos) => (
            <tr
              key={pos.ticker}
              className="border-b border-border/30 hover:bg-bg-input/30"
            >
              <td className="px-2 py-1.5 font-bold text-text-primary">
                {pos.ticker}
              </td>
              <td className="text-right px-2 py-1.5 text-text-primary">
                {pos.quantity}
              </td>
              <td className="text-right px-2 py-1.5 text-text-secondary">
                {formatPrice(pos.avg_cost)}
              </td>
              <td className="text-right px-2 py-1.5 text-text-primary">
                {formatPrice(pos.current_price)}
              </td>
              <td
                className={`text-right px-2 py-1.5 ${
                  pos.unrealized_pnl >= 0 ? "text-gain" : "text-loss"
                }`}
              >
                {formatPnL(pos.unrealized_pnl)}
              </td>
              <td
                className={`text-right px-2 py-1.5 ${
                  pos.pnl_pct >= 0 ? "text-gain" : "text-loss"
                }`}
              >
                {formatPct(pos.pnl_pct)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
