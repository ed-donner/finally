"use client";

import type { Position } from "@/lib/types";
import { formatPercent } from "@/lib/format";

interface PortfolioHeatmapProps {
  positions: Position[];
}

function pnlColor(pct: number): string {
  if (pct > 5) return "#26a641";
  if (pct > 2) return "#2ea043";
  if (pct > 0) return "#1a5c2b";
  if (pct > -2) return "#6e2b28";
  if (pct > -5) return "#b62324";
  return "#f85149";
}

export default function PortfolioHeatmap({ positions }: PortfolioHeatmapProps) {
  if (positions.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-text-muted text-xs">
        No positions to display
      </div>
    );
  }

  const totalValue = positions.reduce((sum, p) => sum + p.market_value, 0);

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-2 border-b border-border">
        <h2 className="text-xs font-bold text-text-secondary uppercase tracking-wider">
          Portfolio Heatmap
        </h2>
      </div>
      <div className="flex-1 flex flex-wrap gap-1 p-2 content-start min-h-0 overflow-hidden">
        {positions.map((pos) => {
          const weight = totalValue > 0 ? pos.market_value / totalValue : 0;
          const minWidth = Math.max(weight * 100, 12);

          return (
            <div
              key={pos.ticker}
              className="rounded flex flex-col items-center justify-center text-xs font-bold"
              style={{
                backgroundColor: pnlColor(pos.pnl_percent),
                flexBasis: `${minWidth}%`,
                flexGrow: weight * 10,
                minHeight: "48px",
                padding: "4px",
              }}
            >
              <span className="text-white text-[11px]">{pos.ticker}</span>
              <span className="text-white/80 text-[9px]">
                {formatPercent(pos.pnl_percent)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
