"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { PriceMap, WatchlistItem } from "@/lib/types";
import { formatPrice, formatPercent } from "@/lib/format";
import { addToWatchlist, removeFromWatchlist } from "@/lib/api";
import Sparkline from "./Sparkline";

interface WatchlistProps {
  items: WatchlistItem[];
  prices: PriceMap;
  getHistory: (ticker: string) => number[];
  selectedTicker: string | null;
  onSelectTicker: (ticker: string) => void;
  onRefresh: () => void;
}

export default function Watchlist({
  items,
  prices,
  getHistory,
  selectedTicker,
  onSelectTicker,
  onRefresh,
}: WatchlistProps) {
  const [addInput, setAddInput] = useState("");
  const prevPricesRef = useRef<Record<string, number>>({});
  const rowRefs = useRef<Record<string, HTMLTableRowElement | null>>({});

  // Apply flash CSS classes directly to DOM elements
  useEffect(() => {
    for (const [ticker, update] of Object.entries(prices)) {
      const prev = prevPricesRef.current[ticker];
      if (prev !== undefined && prev !== update.price) {
        const row = rowRefs.current[ticker];
        if (row) {
          const cls = update.price > prev ? "price-flash-up" : "price-flash-down";
          row.classList.remove("price-flash-up", "price-flash-down");
          // Force reflow to restart animation
          void row.offsetWidth;
          row.classList.add(cls);
        }
      }
      prevPricesRef.current[ticker] = update.price;
    }
  }, [prices]);

  const handleAdd = useCallback(async () => {
    const ticker = addInput.trim().toUpperCase();
    if (!ticker) return;
    await addToWatchlist(ticker);
    setAddInput("");
    onRefresh();
  }, [addInput, onRefresh]);

  const handleRemove = useCallback(
    async (ticker: string, e: React.MouseEvent) => {
      e.stopPropagation();
      await removeFromWatchlist(ticker);
      onRefresh();
    },
    [onRefresh]
  );

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
        <h2 className="text-xs font-bold text-text-secondary uppercase tracking-wider">
          Watchlist
        </h2>
        <div className="flex items-center gap-1">
          <input
            type="text"
            value={addInput}
            onChange={(e) => setAddInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleAdd()}
            placeholder="Add ticker"
            className="w-20 bg-bg-primary border border-border rounded px-1.5 py-0.5 text-xs text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent-blue"
          />
          <button
            onClick={handleAdd}
            className="text-xs text-accent-blue hover:text-accent-yellow px-1"
          >
            +
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-text-muted border-b border-border">
              <th className="text-left px-3 py-1.5 font-normal">Ticker</th>
              <th className="text-right px-2 py-1.5 font-normal">Price</th>
              <th className="text-right px-2 py-1.5 font-normal">Chg%</th>
              <th className="text-right px-3 py-1.5 font-normal">Chart</th>
              <th className="w-6"></th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const update = prices[item.ticker];
              const price = update?.price ?? item.price ?? 0;
              const changePct = update?.change_percent ?? item.change_percent ?? 0;
              const direction = update?.direction ?? item.direction ?? "flat";
              const isSelected = selectedTicker === item.ticker;

              return (
                <tr
                  key={item.ticker}
                  ref={(el) => { rowRefs.current[item.ticker] = el; }}
                  onClick={() => onSelectTicker(item.ticker)}
                  className={`cursor-pointer border-b border-border/50 hover:bg-bg-hover transition-colors ${
                    isSelected ? "bg-bg-hover" : ""
                  }`}
                >
                  <td className="px-3 py-1.5 font-bold text-text-primary">
                    {item.ticker}
                  </td>
                  <td className="text-right px-2 py-1.5 tabular-nums">
                    {formatPrice(price)}
                  </td>
                  <td
                    className={`text-right px-2 py-1.5 tabular-nums ${
                      direction === "up"
                        ? "text-gain"
                        : direction === "down"
                        ? "text-loss"
                        : "text-text-muted"
                    }`}
                  >
                    {formatPercent(changePct)}
                  </td>
                  <td className="text-right px-3 py-1.5">
                    <Sparkline data={getHistory(item.ticker)} />
                  </td>
                  <td className="pr-2">
                    <button
                      onClick={(e) => handleRemove(item.ticker, e)}
                      className="text-text-muted hover:text-loss text-xs leading-none"
                      title="Remove from watchlist"
                    >
                      x
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
