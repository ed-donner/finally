"use client";

import { ChatPanel } from "@/components/chat-panel";
import { ChartPanel } from "@/components/chart-panel";
import { Header } from "@/components/header";
import { HeatmapPanel } from "@/components/heatmap-panel";
import { PnLChart } from "@/components/pnl-chart";
import { PositionsTable } from "@/components/positions-table";
import { TradeBar } from "@/components/trade-bar";
import { ToastViewport } from "@/components/ui/toast-viewport";
import { WatchlistPanel } from "@/components/watchlist-panel";
import { ChatProvider } from "@/lib/store/chat";
import { PortfolioProvider, usePortfolio } from "@/lib/store/portfolio";
import { PricesProvider } from "@/lib/store/prices";
import { ToastProvider } from "@/lib/store/toast";
import { WatchlistProvider, useWatchlist } from "@/lib/store/watchlist";

function ConnectedShell() {
  // After the assistant runs trades or watchlist edits, refresh dependent state.
  const { refresh: refreshPortfolio } = usePortfolio();
  const { refresh: refreshWatchlist } = useWatchlist();
  return (
    <ChatProvider
      onActions={() => {
        refreshPortfolio();
        refreshWatchlist();
      }}
    >
      <Shell />
    </ChatProvider>
  );
}

function Shell() {
  return (
    <div className="relative z-[1] flex flex-col h-screen min-h-0">
      <Header />
      <main
        className="flex-1 min-h-0 grid gap-3 p-3"
        style={{
          gridTemplateColumns: "minmax(280px, 320px) minmax(0, 1fr) minmax(320px, 380px)",
          gridTemplateRows: "minmax(0, 3fr) minmax(0, 2fr) auto",
          gridTemplateAreas: `
            "watchlist chart    chat"
            "watchlist allocate chat"
            "positions positions chat"
          `,
        }}
      >
        <div style={{ gridArea: "watchlist" }} className="min-h-0">
          <WatchlistPanel />
        </div>

        <div style={{ gridArea: "chart" }} className="min-h-0 flex flex-col gap-3">
          <ChartPanel />
        </div>

        <div style={{ gridArea: "allocate" }} className="min-h-0 grid grid-cols-2 gap-3">
          <HeatmapPanel />
          <PnLChart />
        </div>

        <div style={{ gridArea: "positions" }} className="min-h-0 grid grid-cols-[1fr_minmax(380px,420px)] gap-3">
          <PositionsTable />
          <TradeBar />
        </div>

        <div style={{ gridArea: "chat" }} className="min-h-0">
          <ChatPanel />
        </div>
      </main>
      <ToastViewport />
    </div>
  );
}

export default function HomePage() {
  return (
    <ToastProvider>
      <PricesProvider>
        <PortfolioProvider>
          <WatchlistProvider>
            <ConnectedShell />
          </WatchlistProvider>
        </PortfolioProvider>
      </PricesProvider>
    </ToastProvider>
  );
}
