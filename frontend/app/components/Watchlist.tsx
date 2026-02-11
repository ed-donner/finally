"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { PriceUpdate } from "../lib/types";
import { addToWatchlist, fetchWatchlist, removeFromWatchlist } from "../lib/api";
import Sparkline from "./Sparkline";

interface WatchlistProps {
  prices: Record<string, PriceUpdate>;
  sparklines: Record<string, number[]>;
  selectedTicker: string | null;
  onSelectTicker: (ticker: string) => void;
}

export default function Watchlist({ prices, sparklines, selectedTicker, onSelectTicker }: WatchlistProps) {
  const [tickers, setTickers] = useState<string[]>([]);
  const [newTicker, setNewTicker] = useState("");
  const [flashKeys, setFlashKeys] = useState<Record<string, string>>({});
  const prevPrices = useRef<Record<string, number>>({});

  useEffect(() => {
    fetchWatchlist().then((data) => {
      setTickers(data.map((w: { ticker: string }) => w.ticker));
    }).catch(() => {});
  }, []);

  // Detect price changes for flash animation
  useEffect(() => {
    const newFlashes: Record<string, string> = {};
    for (const [ticker, update] of Object.entries(prices)) {
      const prev = prevPrices.current[ticker];
      if (prev !== undefined && prev !== update.price) {
        newFlashes[ticker] = update.price > prev ? "flash-up" : "flash-down";
      }
      prevPrices.current[ticker] = update.price;
    }
    if (Object.keys(newFlashes).length > 0) {
      setFlashKeys(newFlashes);
      const timer = setTimeout(() => setFlashKeys({}), 600);
      return () => clearTimeout(timer);
    }
  }, [prices]);

  const handleAdd = useCallback(async () => {
    const t = newTicker.trim().toUpperCase();
    if (!t) return;
    try {
      await addToWatchlist(t);
      setTickers((prev) => [...prev, t]);
      setNewTicker("");
    } catch { /* ignore duplicates */ }
  }, [newTicker]);

  const handleRemove = useCallback(async (ticker: string) => {
    try {
      await removeFromWatchlist(ticker);
      setTickers((prev) => prev.filter((t) => t !== ticker));
    } catch { /* ignore */ }
  }, []);

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
        <span className="text-accent-yellow font-bold text-xs tracking-wider uppercase">Watchlist</span>
        <div className="flex gap-1">
          <input
            value={newTicker}
            onChange={(e) => setNewTicker(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleAdd()}
            placeholder="Add..."
            className="bg-bg-primary border border-border rounded px-2 py-0.5 text-xs w-16 focus:outline-none focus:border-blue-primary"
          />
        </div>
      </div>
      <div className="flex-1 overflow-y-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-text-secondary border-b border-border">
              <th className="text-left px-3 py-1.5 font-normal">Symbol</th>
              <th className="text-right px-2 py-1.5 font-normal">Price</th>
              <th className="text-right px-2 py-1.5 font-normal">Chg%</th>
              <th className="px-2 py-1.5 font-normal">Trend</th>
              <th className="px-1 py-1.5"></th>
            </tr>
          </thead>
          <tbody>
            {tickers.map((ticker) => {
              const p = prices[ticker];
              const isSelected = ticker === selectedTicker;
              const flash = flashKeys[ticker] || "";
              const changeColor = p?.direction === "up" ? "text-green" : p?.direction === "down" ? "text-red" : "text-text-secondary";

              return (
                <tr
                  key={ticker}
                  onClick={() => onSelectTicker(ticker)}
                  className={`cursor-pointer border-b border-border/50 hover:bg-bg-tertiary transition-colors ${isSelected ? "bg-bg-tertiary" : ""} ${flash}`}
                >
                  <td className="px-3 py-1.5 font-bold text-blue-primary">{ticker}</td>
                  <td className="text-right px-2 py-1.5 font-mono">
                    {p ? `$${p.price.toFixed(2)}` : "--"}
                  </td>
                  <td className={`text-right px-2 py-1.5 ${changeColor}`}>
                    {p ? `${p.change_percent >= 0 ? "+" : ""}${p.change_percent.toFixed(2)}%` : "--"}
                  </td>
                  <td className="px-2 py-1.5">
                    <Sparkline data={sparklines[ticker] || []} />
                  </td>
                  <td className="px-1 py-1.5">
                    <button
                      onClick={(e) => { e.stopPropagation(); handleRemove(ticker); }}
                      className="text-text-secondary hover:text-red text-xs"
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
