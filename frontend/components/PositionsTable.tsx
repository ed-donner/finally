"use client";

import type { Position } from "@/lib/types";
import { formatCurrency, formatPercent, formatPrice } from "@/lib/format";

interface PositionsTableProps {
  positions: Position[];
}

export default function PositionsTable({ positions }: PositionsTableProps) {
  if (positions.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-text-muted text-xs">
        No open positions
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-2 border-b border-border">
        <h2 className="text-xs font-bold text-text-secondary uppercase tracking-wider">
          Positions
        </h2>
      </div>
      <div className="flex-1 overflow-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-text-muted border-b border-border">
              <th className="text-left px-3 py-1.5 font-normal">Ticker</th>
              <th className="text-right px-2 py-1.5 font-normal">Qty</th>
              <th className="text-right px-2 py-1.5 font-normal">Avg Cost</th>
              <th className="text-right px-2 py-1.5 font-normal">Price</th>
              <th className="text-right px-2 py-1.5 font-normal">P&L</th>
              <th className="text-right px-3 py-1.5 font-normal">P&L%</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((pos) => (
              <tr
                key={pos.ticker}
                className="border-b border-border/50 hover:bg-bg-hover transition-colors"
              >
                <td className="px-3 py-1.5 font-bold text-text-primary">
                  {pos.ticker}
                </td>
                <td className="text-right px-2 py-1.5 tabular-nums">
                  {pos.quantity}
                </td>
                <td className="text-right px-2 py-1.5 tabular-nums">
                  {formatPrice(pos.avg_cost)}
                </td>
                <td className="text-right px-2 py-1.5 tabular-nums">
                  {formatPrice(pos.current_price)}
                </td>
                <td
                  className={`text-right px-2 py-1.5 tabular-nums ${
                    pos.unrealized_pnl >= 0 ? "text-gain" : "text-loss"
                  }`}
                >
                  {formatCurrency(pos.unrealized_pnl)}
                </td>
                <td
                  className={`text-right px-3 py-1.5 tabular-nums ${
                    pos.pnl_percent >= 0 ? "text-gain" : "text-loss"
                  }`}
                >
                  {formatPercent(pos.pnl_percent)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
