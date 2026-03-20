"use client";

import { useState } from "react";
import { executeTrade } from "../lib/api";
import type { Portfolio } from "../lib/types";
import type { PriceMap } from "../hooks/usePriceStream";

interface TradeBarProps {
  selectedTicker: string | null;
  prices: PriceMap;
  onTradeComplete: (portfolio: Portfolio) => void;
}

export function TradeBar({ selectedTicker, prices, onTradeComplete }: TradeBarProps) {
  const [ticker, setTicker] = useState("");
  const [quantity, setQuantity] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [lastTrade, setLastTrade] = useState<string | null>(null);

  const effectiveTicker = (ticker.trim().toUpperCase() || selectedTicker || "").toUpperCase();
  const priceData = effectiveTicker ? prices[effectiveTicker] : null;

  const doTrade = async (side: "buy" | "sell") => {
    const t = effectiveTicker;
    const qty = parseFloat(quantity);
    if (!t) { setError("Enter a ticker"); return; }
    if (isNaN(qty) || qty <= 0) { setError("Enter valid quantity"); return; }

    setLoading(true);
    setError(null);
    setLastTrade(null);

    try {
      const result = await executeTrade(t, side, qty);
      onTradeComplete(result.portfolio);
      setLastTrade(`${side.toUpperCase()} ${qty} ${t} @ $${result.price.toFixed(2)}`);
      setQuantity("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Trade failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      className="flex items-center gap-3 px-4 py-2 flex-shrink-0"
      style={{
        borderTop: "1px solid var(--border-color)",
        backgroundColor: "var(--bg-surface)",
      }}
    >
      <span className="text-xs font-semibold tracking-widest uppercase" style={{ color: "var(--text-secondary)" }}>
        Trade
      </span>

      <input
        className="px-2 py-1 text-xs rounded border outline-none font-mono w-20"
        style={{
          backgroundColor: "var(--bg-elevated)",
          borderColor: "var(--border-color)",
          color: "var(--text-primary)",
        }}
        placeholder="TICKER"
        value={ticker}
        onChange={(e) => setTicker(e.target.value.toUpperCase())}
      />

      <input
        className="px-2 py-1 text-xs rounded border outline-none font-mono w-24"
        style={{
          backgroundColor: "var(--bg-elevated)",
          borderColor: "var(--border-color)",
          color: "var(--text-primary)",
        }}
        placeholder="Quantity"
        type="number"
        min="0"
        step="0.01"
        value={quantity}
        onChange={(e) => setQuantity(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && doTrade("buy")}
      />

      {priceData && (
        <span className="text-xs font-mono" style={{ color: "var(--text-secondary)" }}>
          @ ${priceData.price.toFixed(2)}
        </span>
      )}

      <button
        className="px-3 py-1 text-xs rounded font-bold"
        style={{ backgroundColor: "var(--green)", color: "#fff", opacity: loading ? 0.6 : 1 }}
        onClick={() => doTrade("buy")}
        disabled={loading}
      >
        BUY
      </button>

      <button
        className="px-3 py-1 text-xs rounded font-bold"
        style={{ backgroundColor: "var(--red)", color: "#fff", opacity: loading ? 0.6 : 1 }}
        onClick={() => doTrade("sell")}
        disabled={loading}
      >
        SELL
      </button>

      {error && (
        <span className="text-xs" style={{ color: "var(--red)" }}>{error}</span>
      )}
      {lastTrade && !error && (
        <span className="text-xs" style={{ color: "var(--green)" }}>✓ {lastTrade}</span>
      )}
    </div>
  );
}
