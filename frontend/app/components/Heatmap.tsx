"use client";

import { Position } from "../lib/types";

interface HeatmapProps {
  positions: Position[];
}

export default function Heatmap({ positions }: HeatmapProps) {
  if (positions.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-text-secondary text-xs">
        Portfolio heatmap will appear here
      </div>
    );
  }

  const totalValue = positions.reduce((s, p) => s + p.market_value, 0);

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-2 border-b border-border">
        <span className="text-accent-yellow font-bold text-xs tracking-wider uppercase">Heatmap</span>
      </div>
      <div className="flex-1 flex flex-wrap gap-1 p-2 content-start">
        {positions.map((p) => {
          const weight = p.market_value / totalValue;
          const intensity = Math.min(Math.abs(p.pnl_percent) / 5, 1);
          const bg = p.unrealized_pnl >= 0
            ? `rgba(63, 185, 80, ${0.15 + intensity * 0.5})`
            : `rgba(248, 81, 73, ${0.15 + intensity * 0.5})`;
          const minWidth = Math.max(weight * 100, 15);

          return (
            <div
              key={p.ticker}
              style={{ backgroundColor: bg, flexBasis: `${minWidth}%`, flexGrow: 1 }}
              className="rounded px-2 py-1.5 flex flex-col items-center justify-center min-h-[48px] border border-border/30"
            >
              <span className="font-bold text-xs">{p.ticker}</span>
              <span className={`text-xs ${p.unrealized_pnl >= 0 ? "text-green" : "text-red"}`}>
                {p.pnl_percent >= 0 ? "+" : ""}{p.pnl_percent.toFixed(1)}%
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
