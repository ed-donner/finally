"use client";

import { useState, useEffect, useRef } from "react";
import type { WatchlistEntry } from "../lib/types";
import type { PriceMap, SparklineMap } from "../hooks/usePriceStream";
import { Sparkline } from "./Sparkline";
import { addWatchlistTicker, removeWatchlistTicker } from "../lib/api";

interface WatchlistPanelProps {
  watchlist: WatchlistEntry[];
  prices: PriceMap;
  sparklines: SparklineMap;
  selectedTicker: string | null;
  onSelectTicker: (ticker: string) => void;
  onWatchlistChange: () => void;
}

interface TickerRowProps {
  entry: WatchlistEntry;
  price: number | null;
  change_percent: number | null;
  direction: string;
  sparkData: number[];
  isSelected: boolean;
  onSelect: () => void;
  onRemove: () => void;
}

function TickerRow({ entry, price, change_percent, direction, sparkData, isSelected, onSelect, onRemove }: TickerRowProps) {
  const prevPriceRef = useRef<number | null>(null);
  const rowRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (price !== null && prevPriceRef.current !== null && price !== prevPriceRef.current) {
      const el = rowRef.current;
      if (el) {
        el.classList.remove("flash-up", "flash-down");
        void el.offsetWidth; // reflow
        el.classList.add(price > prevPriceRef.current ? "flash-up" : "flash-down");
      }
    }
    prevPriceRef.current = price;
  }, [price]);

  const isPositive = (change_percent ?? 0) >= 0;

  return (
    <div
      ref={rowRef}
      className="flex items-center gap-2 px-3 py-2 cursor-pointer transition-colors rounded"
      style={{
        backgroundColor: isSelected ? "var(--bg-elevated)" : "transparent",
        borderLeft: isSelected ? "2px solid var(--blue-primary)" : "2px solid transparent",
      }}
      onClick={onSelect}
    >
      <div className="flex-1 min-w-0">
        <div className="flex items-center justify-between">
          <span className="font-bold text-sm tracking-wide" style={{ color: "var(--text-primary)" }}>
            {entry.ticker}
          </span>
          <span className="text-sm font-mono font-semibold" style={{ color: "var(--text-primary)" }}>
            {price !== null ? `$${price.toFixed(2)}` : "—"}
          </span>
        </div>
        <div className="flex items-center justify-between mt-0.5">
          <span
            className="text-xs"
            style={{ color: isPositive ? "var(--green)" : "var(--red)" }}
          >
            {change_percent !== null
              ? `${isPositive ? "+" : ""}${change_percent.toFixed(2)}%`
              : "—"}
          </span>
          <button
            className="text-xs opacity-0 group-hover:opacity-100 hover:opacity-100 ml-2"
            style={{ color: "var(--text-secondary)" }}
            onClick={(e) => { e.stopPropagation(); onRemove(); }}
            title="Remove"
          >
            ×
          </button>
        </div>
      </div>
      <div style={{ opacity: 0.85 }}>
        <Sparkline data={sparkData} width={72} height={26} />
      </div>
    </div>
  );
}

export function WatchlistPanel({
  watchlist,
  prices,
  sparklines,
  selectedTicker,
  onSelectTicker,
  onWatchlistChange,
}: WatchlistPanelProps) {
  const [addInput, setAddInput] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const handleAdd = async () => {
    const ticker = addInput.trim().toUpperCase();
    if (!ticker) return;
    setAdding(true);
    setAddError(null);
    try {
      await addWatchlistTicker(ticker);
      setAddInput("");
      onWatchlistChange();
    } catch (e: unknown) {
      setAddError(e instanceof Error ? e.message : "Failed to add");
    } finally {
      setAdding(false);
    }
  };

  const handleRemove = async (ticker: string) => {
    try {
      await removeWatchlistTicker(ticker);
      onWatchlistChange();
    } catch {
      // ignore
    }
  };

  return (
    <div
      className="flex flex-col h-full"
      style={{ borderRight: "1px solid var(--border-color)" }}
    >
      {/* Header */}
      <div
        className="px-3 py-2 flex items-center justify-between flex-shrink-0"
        style={{ borderBottom: "1px solid var(--border-color)", backgroundColor: "var(--bg-surface)" }}
      >
        <span className="text-xs font-semibold tracking-widest uppercase" style={{ color: "var(--text-secondary)" }}>
          Watchlist
        </span>
        <span className="text-xs" style={{ color: "var(--text-secondary)" }}>
          {watchlist.length}
        </span>
      </div>

      {/* Ticker list */}
      <div className="flex-1 overflow-y-auto">
        <div className="group">
          {watchlist.map((entry) => {
            const priceData = prices[entry.ticker];
            return (
              <TickerRow
                key={entry.ticker}
                entry={entry}
                price={priceData?.price ?? entry.price ?? null}
                change_percent={priceData?.change_percent ?? entry.change_percent ?? null}
                direction={priceData?.direction ?? entry.direction ?? "flat"}
                sparkData={sparklines[entry.ticker] ?? []}
                isSelected={selectedTicker === entry.ticker}
                onSelect={() => onSelectTicker(entry.ticker)}
                onRemove={() => handleRemove(entry.ticker)}
              />
            );
          })}
        </div>
      </div>

      {/* Add ticker */}
      <div
        className="px-3 py-2 flex-shrink-0"
        style={{ borderTop: "1px solid var(--border-color)" }}
      >
        <div className="flex gap-1">
          <input
            className="flex-1 px-2 py-1 text-xs rounded border outline-none"
            style={{
              backgroundColor: "var(--bg-elevated)",
              borderColor: addError ? "var(--red)" : "var(--border-color)",
              color: "var(--text-primary)",
            }}
            placeholder="Add ticker…"
            value={addInput}
            onChange={(e) => { setAddInput(e.target.value.toUpperCase()); setAddError(null); }}
            onKeyDown={(e) => e.key === "Enter" && handleAdd()}
          />
          <button
            className="px-2 py-1 text-xs rounded font-semibold"
            style={{ backgroundColor: "var(--blue-primary)", color: "#fff" }}
            onClick={handleAdd}
            disabled={adding}
          >
            +
          </button>
        </div>
        {addError && (
          <p className="text-xs mt-1" style={{ color: "var(--red)" }}>{addError}</p>
        )}
      </div>
    </div>
  );
}
