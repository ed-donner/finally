"use client";

import { useCallback, useEffect, useState } from "react";
import { Portfolio } from "./lib/types";
import { fetchPortfolio } from "./lib/api";
import { useSSE } from "./lib/useSSE";
import Header from "./components/Header";
import Watchlist from "./components/Watchlist";
import PriceChart from "./components/PriceChart";
import Positions from "./components/Positions";
import TradeBar from "./components/TradeBar";
import Heatmap from "./components/Heatmap";
import PnLChart from "./components/PnLChart";
import ChatPanel from "./components/ChatPanel";

export default function Home() {
  const { prices, status, sparklines } = useSSE();
  const [portfolio, setPortfolio] = useState<Portfolio>({ cash: 10000, positions: [], total_value: 10000 });
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);

  const loadPortfolio = useCallback(() => {
    fetchPortfolio().then(setPortfolio).catch(() => {});
  }, []);

  useEffect(() => {
    loadPortfolio();
    const interval = setInterval(loadPortfolio, 5000);
    return () => clearInterval(interval);
  }, [loadPortfolio]);

  return (
    <div className="flex flex-col h-screen overflow-hidden">
      <Header totalValue={portfolio.total_value} cash={portfolio.cash} connectionStatus={status} />

      <div className="flex flex-1 overflow-hidden">
        {/* Left: Watchlist */}
        <div className="w-[320px] border-r border-border flex flex-col bg-bg-secondary">
          <Watchlist
            prices={prices}
            sparklines={sparklines}
            selectedTicker={selectedTicker}
            onSelectTicker={setSelectedTicker}
          />
        </div>

        {/* Center: Chart + Portfolio */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Top: Price Chart */}
          <div className="flex-1 min-h-0 border-b border-border">
            <PriceChart ticker={selectedTicker} sparklines={sparklines} prices={prices} />
          </div>

          {/* Bottom panels */}
          <div className="h-[280px] flex border-b border-border">
            {/* Positions table */}
            <div className="flex-1 border-r border-border overflow-hidden">
              <Positions positions={portfolio.positions} />
            </div>
            {/* Heatmap */}
            <div className="w-[260px] border-r border-border overflow-hidden">
              <Heatmap positions={portfolio.positions} />
            </div>
            {/* P&L Chart */}
            <div className="w-[260px] overflow-hidden">
              <PnLChart />
            </div>
          </div>

          {/* Trade bar */}
          <TradeBar selectedTicker={selectedTicker} onTradeComplete={loadPortfolio} />
        </div>

        {/* Right: Chat */}
        <div className="w-[300px]">
          <ChatPanel onTradeExecuted={loadPortfolio} />
        </div>
      </div>
    </div>
  );
}
