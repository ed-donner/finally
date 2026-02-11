"use client";

import type { Position } from "@/stores/portfolio-store";

const currencyFormat = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});

function formatPnl(value: number): string {
  const prefix = value >= 0 ? "+" : "";
  return prefix + currencyFormat.format(value);
}

function formatPercent(value: number): string {
  const prefix = value >= 0 ? "+" : "";
  return prefix + value.toFixed(2) + "%";
}

export function PositionsTable({ positions }: { positions: Position[] }) {
  if (positions.length === 0) {
    return (
      <div className="h-full w-full flex items-center justify-center">
        <span className="text-text-muted font-mono text-xs">No positions</span>
      </div>
    );
  }

  return (
    <div className="overflow-y-auto">
      <table className="w-full font-mono text-xs">
        <thead>
          <tr className="text-text-muted uppercase tracking-wider">
            <th className="text-left py-1 px-2 font-normal">Ticker</th>
            <th className="text-right py-1 px-2 font-normal">Qty</th>
            <th className="text-right py-1 px-2 font-normal">Avg Cost</th>
            <th className="text-right py-1 px-2 font-normal">Price</th>
            <th className="text-right py-1 px-2 font-normal">P&L</th>
            <th className="text-right py-1 px-2 font-normal">%</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => (
            <tr key={p.ticker} className="border-t border-terminal-border">
              <td className="text-left py-1 px-2 text-text-primary font-bold">
                {p.ticker}
              </td>
              <td className="text-right py-1 px-2 text-text-secondary">
                {p.quantity}
              </td>
              <td className="text-right py-1 px-2 text-text-secondary">
                {currencyFormat.format(p.avg_cost)}
              </td>
              <td className="text-right py-1 px-2 text-text-primary">
                {currencyFormat.format(p.current_price)}
              </td>
              <td
                className={`text-right py-1 px-2 ${p.unrealized_pnl >= 0 ? "text-price-up" : "text-price-down"}`}
              >
                {formatPnl(p.unrealized_pnl)}
              </td>
              <td
                className={`text-right py-1 px-2 ${p.unrealized_pnl_percent >= 0 ? "text-price-up" : "text-price-down"}`}
              >
                {formatPercent(p.unrealized_pnl_percent)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
