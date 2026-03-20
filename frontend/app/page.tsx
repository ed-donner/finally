"use client";

import { useState, useEffect, useCallback } from "react";
import { usePriceStream } from "../hooks/usePriceStream";
import { fetchWatchlist, fetchPortfolio, fetchPortfolioHistory } from "../lib/api";
import type { WatchlistEntry, Portfolio, PortfolioSnapshot } from "../lib/types";
import { ConnectionDot } from "../components/ConnectionDot";
import { WatchlistPanel } from "../components/WatchlistPanel";
import { MainChart } from "../components/MainChart";
import { PortfolioPanel } from "../components/PortfolioPanel";
import { PositionsTable } from "../components/PositionsTable";
import { TradeBar } from "../components/TradeBar";
import { ChatPanel } from "../components/ChatPanel";

export default function TradingWorkstation() {
  const { prices, sparklines, status } = usePriceStream();
  const [watchlist, setWatchlist] = useState<WatchlistEntry[]>([]);
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [history, setHistory] = useState<PortfolioSnapshot[]>([]);
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<"positions" | "heatmap">("heatmap");

  const loadWatchlist = useCallback(() => {
    fetchWatchlist().then(setWatchlist).catch(console.error);
  }, []);

  const loadPortfolio = useCallback(() => {
    fetchPortfolio().then(setPortfolio).catch(console.error);
    fetchPortfolioHistory().then(setHistory).catch(console.error);
  }, []);

  useEffect(() => {
    loadWatchlist();
    loadPortfolio();
  }, [loadWatchlist, loadPortfolio]);

  // Refresh portfolio every 30s
  useEffect(() => {
    const interval = setInterval(loadPortfolio, 30000);
    return () => clearInterval(interval);
  }, [loadPortfolio]);

  const totalValue = portfolio?.total_value ?? 10000;
  const cash = portfolio?.cash_balance ?? 10000;
  const totalPnl = totalValue - 10000;

  return (
    <div
      className="flex flex-col h-screen overflow-hidden"
      style={{ backgroundColor: "var(--bg-base)", color: "var(--text-primary)" }}
    >
      {/* ── Header ── */}
      <header
        className="flex items-center gap-4 px-4 py-2 flex-shrink-0"
        style={{
          borderBottom: "1px solid var(--border-color)",
          backgroundColor: "var(--bg-surface)",
          height: 44,
        }}
      >
        {/* Logo */}
        <div className="flex items-center gap-2 flex-shrink-0">
          <span
            className="font-bold text-base tracking-wider"
            style={{ color: "var(--accent-yellow)" }}
          >
            Fin<span style={{ color: "var(--blue-primary)" }}>Ally</span>
          </span>
          <span className="text-xs" style={{ color: "var(--text-secondary)" }}>
            AI Trading Workstation
          </span>
        </div>

        <div
          className="flex-1 flex items-center justify-center gap-6"
        >
          {/* Total Value */}
          <div className="flex items-center gap-2">
            <span className="text-xs" style={{ color: "var(--text-secondary)" }}>Portfolio</span>
            <span className="font-mono font-bold text-sm">${totalValue.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
          </div>

          {/* P&L */}
          <div className="flex items-center gap-1">
            <span className="text-xs" style={{ color: "var(--text-secondary)" }}>P&L</span>
            <span
              className="font-mono text-sm font-semibold"
              style={{ color: totalPnl >= 0 ? "var(--green)" : "var(--red)" }}
            >
              {totalPnl >= 0 ? "+" : ""}${totalPnl.toFixed(2)}
            </span>
          </div>

          {/* Cash */}
          <div className="flex items-center gap-2">
            <span className="text-xs" style={{ color: "var(--text-secondary)" }}>Cash</span>
            <span className="font-mono text-sm">${cash.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
          </div>
        </div>

        {/* Connection status */}
        <div className="flex-shrink-0">
          <ConnectionDot status={status} />
        </div>
      </header>

      {/* ── Main body ── */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left: Watchlist */}
        <div className="flex-shrink-0 overflow-hidden" style={{ width: 220 }}>
          <WatchlistPanel
            watchlist={watchlist}
            prices={prices}
            sparklines={sparklines}
            selectedTicker={selectedTicker}
            onSelectTicker={setSelectedTicker}
            onWatchlistChange={loadWatchlist}
          />
        </div>

        {/* Center: Chart + bottom panels */}
        <div className="flex flex-col flex-1 overflow-hidden">
          {/* Main chart */}
          <div
            className="flex-1 overflow-hidden"
            style={{ borderBottom: "1px solid var(--border-color)", minHeight: 200 }}
          >
            <MainChart
              ticker={selectedTicker}
              prices={prices}
              sparklines={sparklines}
            />
          </div>

          {/* Bottom: portfolio panel + trade bar */}
          <div className="flex-shrink-0" style={{ height: 240 }}>
            {/* Tab bar */}
            <div
              className="flex items-center gap-0 flex-shrink-0"
              style={{ borderBottom: "1px solid var(--border-color)", backgroundColor: "var(--bg-surface)" }}
            >
              {(["heatmap", "positions"] as const).map((tab) => (
                <button
                  key={tab}
                  className="px-4 py-1.5 text-xs font-semibold tracking-wide capitalize"
                  style={{
                    borderBottom: activeTab === tab ? "2px solid var(--blue-primary)" : "2px solid transparent",
                    color: activeTab === tab ? "var(--text-primary)" : "var(--text-secondary)",
                    backgroundColor: "transparent",
                  }}
                  onClick={() => setActiveTab(tab)}
                >
                  {tab === "heatmap" ? "Heatmap & P&L" : "Positions"}
                </button>
              ))}
            </div>

            <div className="overflow-hidden" style={{ height: 196 }}>
              {activeTab === "heatmap" ? (
                <PortfolioPanel
                  portfolio={portfolio}
                  history={history}
                  prices={prices}
                  onSelectTicker={setSelectedTicker}
                />
              ) : (
                <PositionsTable
                  positions={portfolio?.positions ?? []}
                  onSelectTicker={setSelectedTicker}
                />
              )}
            </div>
          </div>

          {/* Trade bar */}
          <TradeBar
            selectedTicker={selectedTicker}
            prices={prices}
            onTradeComplete={(p) => {
              setPortfolio(p);
              loadPortfolio();
            }}
          />
        </div>

        {/* Right: AI Chat */}
        <div className="flex-shrink-0 overflow-hidden" style={{ width: 280 }}>
          <ChatPanel onPortfolioUpdate={loadPortfolio} />
        </div>
      </div>
    </div>
  );
}
