"use client";

import { useEffect } from "react";
import { usePriceStream } from "@/hooks/use-price-stream";
import { usePortfolioStore } from "@/stores/portfolio-store";
import { useWatchlistStore } from "@/stores/watchlist-store";
import { Header } from "@/components/layout/Header";
import { TerminalGrid } from "@/components/layout/TerminalGrid";
import { WatchlistPanel } from "@/components/panels/WatchlistPanel";
import { ChartPanel } from "@/components/panels/ChartPanel";
import { PortfolioPanel } from "@/components/panels/PortfolioPanel";
import { PositionsTable } from "@/components/portfolio/PositionsTable";
import { TradeBar } from "@/components/portfolio/TradeBar";
import { ChatPanel } from "@/components/panels/ChatPanel";

export default function Home() {
  usePriceStream();

  const fetchPortfolio = usePortfolioStore((s) => s.fetchPortfolio);
  const fetchHistory = usePortfolioStore((s) => s.fetchHistory);
  const positions = usePortfolioStore((s) => s.positions);
  const fetchWatchlist = useWatchlistStore((s) => s.fetchWatchlist);

  useEffect(() => {
    fetchPortfolio();
    fetchHistory();
    fetchWatchlist();
  }, [fetchPortfolio, fetchHistory, fetchWatchlist]);

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
        <div className="col-span-3 bg-terminal-surface flex flex-col">
          <div className="px-3 pt-3 pb-1">
            <span className="text-text-muted font-mono text-xs uppercase tracking-wider">
              Positions
            </span>
          </div>
          <div className="flex-1 min-h-0 overflow-y-auto">
            <PositionsTable positions={positions} />
          </div>
          <TradeBar />
        </div>
      </TerminalGrid>
    </div>
  );
}
