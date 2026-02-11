"use client";

import { usePortfolioStore } from "@/stores/portfolio-store";
import { Heatmap } from "@/components/portfolio/Heatmap";
import { PnlChart } from "@/components/portfolio/PnlChart";

export function PortfolioPanel() {
  const positions = usePortfolioStore((s) => s.positions);
  const snapshots = usePortfolioStore((s) => s.snapshots);

  return (
    <div className="h-full w-full flex flex-col bg-terminal-surface">
      <div className="px-3 pt-3 pb-1">
        <span className="text-text-muted font-mono text-xs uppercase tracking-wider">
          Portfolio
        </span>
      </div>
      <div className="flex-1 flex flex-col min-h-0">
        <div className="flex-1 min-h-0 px-1">
          <Heatmap positions={positions} />
        </div>
        <div className="flex-1 min-h-0">
          <PnlChart snapshots={snapshots} />
        </div>
      </div>
    </div>
  );
}
