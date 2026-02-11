"use client";

import { useState, useEffect } from "react";
import { usePortfolioStore } from "@/stores/portfolio-store";
import { useWatchlistStore } from "@/stores/watchlist-store";

export function TradeBar() {
  const [ticker, setTicker] = useState("");
  const [quantity, setQuantity] = useState("");

  const executeTrade = usePortfolioStore((s) => s.executeTrade);
  const tradeError = usePortfolioStore((s) => s.tradeError);
  const tradeLoading = usePortfolioStore((s) => s.tradeLoading);
  const selectedTicker = useWatchlistStore((s) => s.selectedTicker);

  useEffect(() => {
    setTicker(selectedTicker ?? "");
  }, [selectedTicker]);

  async function handleTrade(side: "buy" | "sell") {
    const qty = parseFloat(quantity);
    if (!ticker.trim() || isNaN(qty) || qty <= 0) return;
    await executeTrade(ticker.toUpperCase().trim(), side, qty);
    if (!usePortfolioStore.getState().tradeError) {
      setQuantity("");
    }
  }

  return (
    <div className="p-2 border-t border-terminal-border">
      <div className="flex items-center gap-1">
        <input
          type="text"
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder="TICKER"
          className="bg-terminal-bg border border-terminal-border rounded px-2 py-1 font-mono text-xs text-text-primary placeholder:text-text-muted focus:outline-none focus:border-brand-blue w-20"
        />
        <input
          type="number"
          value={quantity}
          onChange={(e) => setQuantity(e.target.value)}
          placeholder="QTY"
          step="any"
          className="bg-terminal-bg border border-terminal-border rounded px-2 py-1 font-mono text-xs text-text-primary placeholder:text-text-muted focus:outline-none focus:border-brand-blue w-16"
        />
        <button
          onClick={() => handleTrade("buy")}
          disabled={tradeLoading}
          className="bg-price-up/20 text-price-up border border-price-up/30 rounded px-2 py-1 font-mono text-xs font-bold hover:bg-price-up/30 transition-colors disabled:opacity-50"
        >
          BUY
        </button>
        <button
          onClick={() => handleTrade("sell")}
          disabled={tradeLoading}
          className="bg-price-down/20 text-price-down border border-price-down/30 rounded px-2 py-1 font-mono text-xs font-bold hover:bg-price-down/30 transition-colors disabled:opacity-50"
        >
          SELL
        </button>
      </div>
      {tradeError && (
        <p className="text-price-down text-xs font-mono mt-1">{tradeError}</p>
      )}
    </div>
  );
}
