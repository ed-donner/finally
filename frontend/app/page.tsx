"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { usePrices } from "@/lib/use-prices";
import { getPortfolio, getWatchlist } from "@/lib/api";
import type { Portfolio, WatchlistItem } from "@/lib/types";
import Header from "@/components/Header";
import Watchlist from "@/components/Watchlist";
import PriceChart from "@/components/PriceChart";
import PositionsTable from "@/components/PositionsTable";
import PortfolioHeatmap from "@/components/PortfolioHeatmap";
import PnlChart from "@/components/PnlChart";
import TradeBar from "@/components/TradeBar";
import ChatPanel from "@/components/ChatPanel";

async function fetchPortfolio(setter: (p: Portfolio) => void) {
  try {
    const data = await getPortfolio();
    setter(data);
  } catch {
    // Retry on next interval
  }
}

async function fetchWatchlist(setter: (w: WatchlistItem[]) => void) {
  try {
    const data = await getWatchlist();
    setter(data);
  } catch {
    // Retry on next interval
  }
}

export default function Home() {
  const { prices, status, getHistory } = usePrices();
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [userSelectedTicker, setUserSelectedTicker] = useState<string | null>(null);

  // Derive the effective selected ticker: user selection, or first watchlist item
  const selectedTicker = useMemo(() => {
    if (userSelectedTicker) return userSelectedTicker;
    return watchlist.length > 0 ? watchlist[0].ticker : null;
  }, [userSelectedTicker, watchlist]);

  const refreshWatchlist = useCallback(() => {
    fetchWatchlist(setWatchlist);
  }, []);

  const refreshAll = useCallback(() => {
    fetchPortfolio(setPortfolio);
    fetchWatchlist(setWatchlist);
  }, []);

  // Periodic data refresh via subscription to external system (API)
  useEffect(() => {
    fetchPortfolio(setPortfolio);
    fetchWatchlist(setWatchlist);
    const interval = setInterval(() => {
      fetchPortfolio(setPortfolio);
      fetchWatchlist(setWatchlist);
    }, 5000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="h-screen flex flex-col overflow-hidden">
      <Header
        totalValue={portfolio?.total_value ?? 10000}
        cashBalance={portfolio?.cash_balance ?? 10000}
        status={status}
      />

      <div className="flex-1 flex min-h-0">
        {/* Left column: Watchlist + Trade bar */}
        <div className="w-80 flex flex-col border-r border-border bg-bg-panel shrink-0">
          <div className="flex-1 min-h-0">
            <Watchlist
              items={watchlist}
              prices={prices}
              getHistory={getHistory}
              selectedTicker={selectedTicker}
              onSelectTicker={setUserSelectedTicker}
              onRefresh={refreshWatchlist}
            />
          </div>
          <TradeBar prices={prices} onTradeExecuted={refreshAll} />
        </div>

        {/* Center: Charts + Positions */}
        <div className="flex-1 flex flex-col min-w-0">
          {/* Top row: Price Chart + Portfolio Heatmap */}
          <div className="flex-1 flex min-h-0">
            <div className="flex-[2] border-r border-border min-w-0">
              <PriceChart ticker={selectedTicker} getHistory={getHistory} />
            </div>
            <div className="flex-1 min-w-0">
              <PortfolioHeatmap positions={portfolio?.positions ?? []} />
            </div>
          </div>

          {/* Bottom row: Positions + P&L Chart */}
          <div className="h-[40%] flex border-t border-border min-h-0">
            <div className="flex-1 border-r border-border min-w-0">
              <PositionsTable positions={portfolio?.positions ?? []} />
            </div>
            <div className="flex-1 min-w-0">
              <PnlChart />
            </div>
          </div>
        </div>

        {/* Right column: Chat panel */}
        <div className="w-80 shrink-0">
          <ChatPanel onTradeExecuted={refreshAll} />
        </div>
      </div>
    </div>
  );
}
