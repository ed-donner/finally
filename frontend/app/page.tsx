"use client";

import { useEffect, useCallback } from "react";
import { useStore } from "@/lib/store";
import { useSSE } from "@/lib/useSSE";
import { getPortfolio } from "@/lib/api";
import { Header } from "@/components/Header";
import { Watchlist } from "@/components/Watchlist";
import { MainChart } from "@/components/MainChart";
import { PortfolioHeatmap } from "@/components/PortfolioHeatmap";
import { PnLChart } from "@/components/PnLChart";
import { PositionsTable } from "@/components/PositionsTable";
import { TradeBar } from "@/components/TradeBar";
import { ChatPanel } from "@/components/ChatPanel";

export default function Home() {
  const setPortfolio = useStore((s) => s.setPortfolio);
  const chatOpen = useStore((s) => s.chatOpen);

  // Connect SSE for live prices
  useSSE();

  // Load portfolio data
  const loadPortfolio = useCallback(async () => {
    try {
      const data = await getPortfolio();
      setPortfolio(data);
    } catch {
      // retry on next interval
    }
  }, [setPortfolio]);

  useEffect(() => {
    loadPortfolio();
    const interval = setInterval(loadPortfolio, 5000);
    return () => clearInterval(interval);
  }, [loadPortfolio]);

  return (
    <div className="flex flex-col h-screen bg-bg-primary">
      <Header />

      <div className="flex flex-1 min-h-0">
        {/* Main content area */}
        <div className="flex flex-1 min-w-0">
          {/* Left sidebar - Watchlist */}
          <div className="w-56 flex-shrink-0 border-r border-border bg-bg-primary">
            <Watchlist />
          </div>

          {/* Center content */}
          <div className="flex-1 flex flex-col min-w-0">
            {/* Top row: Main chart + Heatmap */}
            <div className="flex flex-1 min-h-0">
              <div className="flex-1 min-w-0 border-b border-border">
                <MainChart />
              </div>
              <div className="w-72 flex-shrink-0 border-l border-b border-border p-2">
                <div className="h-full flex flex-col">
                  <h3 className="text-xs font-bold text-text-secondary uppercase tracking-wider mb-1 px-1">
                    Portfolio Heatmap
                  </h3>
                  <div className="flex-1">
                    <PortfolioHeatmap />
                  </div>
                </div>
              </div>
            </div>

            {/* Bottom row: P&L chart + Positions */}
            <div className="flex h-52 flex-shrink-0">
              <div className="flex-1 min-w-0 border-r border-border p-2">
                <div className="h-full flex flex-col">
                  <h3 className="text-xs font-bold text-text-secondary uppercase tracking-wider mb-1 px-1">
                    Portfolio P&L
                  </h3>
                  <div className="flex-1">
                    <PnLChart />
                  </div>
                </div>
              </div>
              <div className="flex-1 min-w-0 p-2">
                <div className="h-full flex flex-col">
                  <h3 className="text-xs font-bold text-text-secondary uppercase tracking-wider mb-1 px-1">
                    Positions
                  </h3>
                  <div className="flex-1 overflow-hidden">
                    <PositionsTable />
                  </div>
                </div>
              </div>
            </div>

            {/* Trade bar */}
            <TradeBar />
          </div>
        </div>

        {/* Chat panel - right sidebar */}
        {chatOpen && (
          <div className="w-80 flex-shrink-0">
            <ChatPanel />
          </div>
        )}
      </div>
    </div>
  );
}
