"use client";

import { useEffect, useState, useCallback } from "react";
import { useStore } from "@/lib/store";
import { getWatchlist, addToWatchlist, removeFromWatchlist } from "@/lib/api";
import { formatPrice, formatPct } from "@/lib/format";
import { Sparkline } from "./Sparkline";

export function Watchlist() {
  const watchlist = useStore((s) => s.watchlist);
  const setWatchlist = useStore((s) => s.setWatchlist);
  const prices = useStore((s) => s.prices);
  const priceHistory = useStore((s) => s.priceHistory);
  const flashState = useStore((s) => s.flashState);
  const clearFlash = useStore((s) => s.clearFlash);
  const selectedTicker = useStore((s) => s.selectedTicker);
  const setSelectedTicker = useStore((s) => s.setSelectedTicker);
  const [newTicker, setNewTicker] = useState("");

  const loadWatchlist = useCallback(async () => {
    try {
      const data = await getWatchlist();
      setWatchlist(data);
      if (data.length > 0 && !selectedTicker) {
        setSelectedTicker(data[0].ticker);
      }
    } catch {
      // retry later
    }
  }, [setWatchlist, selectedTicker, setSelectedTicker]);

  useEffect(() => {
    loadWatchlist();
  }, [loadWatchlist]);

  const handleAdd = async () => {
    const ticker = newTicker.trim().toUpperCase();
    if (!ticker) return;
    try {
      await addToWatchlist(ticker);
      setNewTicker("");
      loadWatchlist();
    } catch {
      // ignore
    }
  };

  const handleRemove = async (ticker: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await removeFromWatchlist(ticker);
      loadWatchlist();
    } catch {
      // ignore
    }
  };

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
        <h2 className="text-xs font-bold text-text-secondary uppercase tracking-wider">
          Watchlist
        </h2>
      </div>

      <div className="flex-1 overflow-y-auto">
        {watchlist.map((entry) => {
          const livePrice = prices[entry.ticker];
          const price = livePrice?.price ?? entry.price;
          const prevPrice = livePrice?.previous_price ?? entry.previous_price;
          const change =
            price != null && prevPrice != null ? price - prevPrice : null;
          const changePct =
            change != null && prevPrice ? (change / prevPrice) * 100 : null;
          const direction = livePrice?.direction ?? entry.direction;
          const flash = flashState[entry.ticker];
          const history = priceHistory[entry.ticker] || [];
          const isSelected = selectedTicker === entry.ticker;

          return (
            <div
              key={entry.ticker}
              onClick={() => setSelectedTicker(entry.ticker)}
              onAnimationEnd={() => clearFlash(entry.ticker)}
              className={`flex items-center justify-between px-3 py-2 cursor-pointer border-b border-border/50 hover:bg-bg-input/50 transition-colors ${
                isSelected ? "bg-bg-input" : ""
              } ${flash === "up" ? "flash-up" : flash === "down" ? "flash-down" : ""}`}
            >
              <div className="flex flex-col gap-0.5 min-w-0">
                <span className="text-sm font-bold text-text-primary">
                  {entry.ticker}
                </span>
                <Sparkline data={history} width={64} height={20} />
              </div>
              <div className="flex flex-col items-end gap-0.5">
                <span className="text-sm font-medium text-text-primary">
                  {price != null ? formatPrice(price) : "--"}
                </span>
                {changePct != null && (
                  <span
                    className={`text-xs ${
                      direction === "up"
                        ? "text-gain"
                        : direction === "down"
                          ? "text-loss"
                          : "text-text-secondary"
                    }`}
                  >
                    {formatPct(changePct)}
                  </span>
                )}
                <button
                  onClick={(e) => handleRemove(entry.ticker, e)}
                  className="text-[10px] text-text-secondary hover:text-loss transition-colors"
                >
                  remove
                </button>
              </div>
            </div>
          );
        })}
      </div>

      <div className="flex items-center gap-1 p-2 border-t border-border">
        <input
          value={newTicker}
          onChange={(e) => setNewTicker(e.target.value.toUpperCase())}
          onKeyDown={(e) => e.key === "Enter" && handleAdd()}
          placeholder="Add ticker..."
          maxLength={5}
          className="flex-1 bg-bg-input text-xs text-text-primary px-2 py-1.5 rounded border border-border focus:border-accent-blue outline-none"
        />
        <button
          onClick={handleAdd}
          className="px-2 py-1.5 text-xs bg-accent-blue text-white rounded hover:opacity-80 transition-opacity"
        >
          +
        </button>
      </div>
    </div>
  );
}
