"use client";

import { useState } from "react";
import { useWatchlistStore } from "@/stores/watchlist-store";
import { PriceCell } from "@/components/ui/PriceCell";

export function WatchlistPanel() {
  const tickers = useWatchlistStore((s) => s.tickers);
  const selectedTicker = useWatchlistStore((s) => s.selectedTicker);
  const loading = useWatchlistStore((s) => s.loading);
  const selectTicker = useWatchlistStore((s) => s.selectTicker);
  const addTicker = useWatchlistStore((s) => s.addTicker);
  const removeTicker = useWatchlistStore((s) => s.removeTicker);

  const [input, setInput] = useState("");

  const handleAdd = () => {
    const trimmed = input.trim();
    if (trimmed) {
      addTicker(trimmed);
      setInput("");
    }
  };

  return (
    <div className="h-full flex flex-col p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="font-mono text-xs uppercase tracking-wider text-text-muted">
          Watchlist
        </span>
        <span className="font-mono text-xs text-text-muted">
          {tickers.length}
        </span>
      </div>

      <div className="flex items-center gap-1 mb-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value.toUpperCase())}
          onKeyDown={(e) => e.key === "Enter" && handleAdd()}
          placeholder="TICKER"
          className="flex-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1 font-mono text-xs text-text-primary placeholder:text-text-muted focus:outline-none focus:border-brand-blue"
        />
        <button
          onClick={handleAdd}
          className="bg-terminal-bg border border-terminal-border rounded px-2 py-1 font-mono text-xs text-text-muted hover:text-brand-blue hover:border-brand-blue transition-colors"
        >
          +
        </button>
      </div>

      <div className="flex-1 overflow-y-auto -mx-3">
        {loading && tickers.length === 0 && (
          <div className="flex items-center justify-center h-full">
            <span className="font-mono text-xs text-text-muted">Loading...</span>
          </div>
        )}
        {!loading && tickers.length === 0 && (
          <div className="flex items-center justify-center h-full">
            <span className="font-mono text-xs text-text-muted">No tickers</span>
          </div>
        )}
        {tickers.map((ticker) => (
          <PriceCell
            key={ticker}
            ticker={ticker}
            isSelected={selectedTicker === ticker}
            onSelect={selectTicker}
            onRemove={removeTicker}
          />
        ))}
      </div>
    </div>
  );
}
