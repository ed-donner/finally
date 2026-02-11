"use client";

import { Position } from "../lib/types";

interface PositionsProps {
  positions: Position[];
}

export default function Positions({ positions }: PositionsProps) {
  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-2 border-b border-border">
        <span className="text-accent-yellow font-bold text-xs tracking-wider uppercase">Positions</span>
      </div>
      <div className="flex-1 overflow-y-auto">
        {positions.length === 0 ? (
          <div className="flex items-center justify-center h-full text-text-secondary text-xs">
            No positions yet. Execute a trade to get started.
          </div>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-text-secondary border-b border-border">
                <th className="text-left px-3 py-1.5 font-normal">Symbol</th>
                <th className="text-right px-2 py-1.5 font-normal">Qty</th>
                <th className="text-right px-2 py-1.5 font-normal">Avg Cost</th>
                <th className="text-right px-2 py-1.5 font-normal">Price</th>
                <th className="text-right px-2 py-1.5 font-normal">P&L</th>
                <th className="text-right px-2 py-1.5 font-normal">%</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => {
                const pnlColor = p.unrealized_pnl >= 0 ? "text-green" : "text-red";
                return (
                  <tr key={p.ticker} className="border-b border-border/50 hover:bg-bg-tertiary">
                    <td className="px-3 py-1.5 font-bold text-blue-primary">{p.ticker}</td>
                    <td className="text-right px-2 py-1.5">{p.quantity}</td>
                    <td className="text-right px-2 py-1.5">${p.avg_cost.toFixed(2)}</td>
                    <td className="text-right px-2 py-1.5">${p.current_price.toFixed(2)}</td>
                    <td className={`text-right px-2 py-1.5 ${pnlColor}`}>
                      {p.unrealized_pnl >= 0 ? "+" : ""}${p.unrealized_pnl.toFixed(2)}
                    </td>
                    <td className={`text-right px-2 py-1.5 ${pnlColor}`}>
                      {p.pnl_percent >= 0 ? "+" : ""}{p.pnl_percent.toFixed(2)}%
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
