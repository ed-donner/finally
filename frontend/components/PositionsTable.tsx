"use client";

import type { Position } from "../lib/types";

interface PositionsTableProps {
  positions: Position[];
  onSelectTicker: (ticker: string) => void;
}

export function PositionsTable({ positions, onSelectTicker }: PositionsTableProps) {
  if (positions.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-xs" style={{ color: "var(--text-secondary)" }}>
        No open positions
      </div>
    );
  }

  return (
    <div className="overflow-x-auto h-full">
      <table className="w-full text-xs">
        <thead>
          <tr style={{ borderBottom: "1px solid var(--border-color)", backgroundColor: "var(--bg-surface)" }}>
            {["Ticker", "Qty", "Avg Cost", "Current", "Value", "P&L", "%"].map((h) => (
              <th
                key={h}
                className="px-3 py-1.5 text-left font-semibold tracking-wide"
                style={{ color: "var(--text-secondary)" }}
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {positions.map((pos) => (
            <tr
              key={pos.ticker}
              className="cursor-pointer transition-colors hover:opacity-80"
              style={{ borderBottom: "1px solid var(--border-color)20" }}
              onClick={() => onSelectTicker(pos.ticker)}
            >
              <td className="px-3 py-1.5 font-bold tracking-wide" style={{ color: "var(--blue-primary)" }}>
                {pos.ticker}
              </td>
              <td className="px-3 py-1.5 font-mono">{pos.quantity.toFixed(4)}</td>
              <td className="px-3 py-1.5 font-mono">${pos.avg_cost.toFixed(2)}</td>
              <td className="px-3 py-1.5 font-mono">${pos.current_price.toFixed(2)}</td>
              <td className="px-3 py-1.5 font-mono">${pos.market_value.toFixed(2)}</td>
              <td
                className="px-3 py-1.5 font-mono font-semibold"
                style={{ color: pos.unrealized_pnl >= 0 ? "var(--green)" : "var(--red)" }}
              >
                {pos.unrealized_pnl >= 0 ? "+" : ""}${pos.unrealized_pnl.toFixed(2)}
              </td>
              <td
                className="px-3 py-1.5 font-mono font-semibold"
                style={{ color: pos.pnl_percent >= 0 ? "var(--green)" : "var(--red)" }}
              >
                {pos.pnl_percent >= 0 ? "+" : ""}{pos.pnl_percent.toFixed(2)}%
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
