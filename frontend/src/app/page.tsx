"use client";

import { useEffect } from "react";
import { usePriceStream } from "@/hooks/use-price-stream";
import { usePortfolioStore } from "@/stores/portfolio-store";
import { Header } from "@/components/layout/Header";
import { TerminalGrid } from "@/components/layout/TerminalGrid";
import { WatchlistPanel } from "@/components/panels/WatchlistPanel";
import { ChartPanel } from "@/components/panels/ChartPanel";
import { PortfolioPanel } from "@/components/panels/PortfolioPanel";
import { ChatPanel } from "@/components/panels/ChatPanel";

export default function Home() {
  usePriceStream();

  const fetchPortfolio = usePortfolioStore((s) => s.fetchPortfolio);

  useEffect(() => {
    fetchPortfolio();
  }, [fetchPortfolio]);

  return (
    <div className="h-screen flex flex-col bg-terminal-bg">
      <Header />
      <TerminalGrid>
        <div className="col-span-3 row-span-2 bg-terminal-surface">
          <WatchlistPanel />
        </div>
        <div className="col-span-6 bg-terminal-surface">
          <ChartPanel />
        </div>
        <div className="col-span-3 row-span-2 bg-terminal-surface">
          <ChatPanel />
        </div>
        <div className="col-span-3 bg-terminal-surface">
          <PortfolioPanel />
        </div>
        <div className="col-span-3 bg-terminal-surface p-3">
          <span className="text-text-muted font-mono text-xs uppercase tracking-wider">
            Positions
          </span>
        </div>
      </TerminalGrid>
    </div>
  );
}
