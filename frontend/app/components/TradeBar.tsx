"use client";

import { useState } from "react";
import { executeTrade } from "../lib/api";

interface TradeBarProps {
  selectedTicker: string | null;
  onTradeComplete: () => void;
}

export default function TradeBar({ selectedTicker, onTradeComplete }: TradeBarProps) {
  const [ticker, setTicker] = useState("");
  const [quantity, setQuantity] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const activeTicker = ticker.toUpperCase() || selectedTicker || "";

  const handleTrade = async (side: "buy" | "sell") => {
    if (!activeTicker || !quantity) return;
    setError("");
    setLoading(true);
    try {
      await executeTrade(activeTicker, side, parseFloat(quantity));
      setQuantity("");
      setTicker("");
      onTradeComplete();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Trade failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="border-t border-border bg-bg-secondary px-3 py-2">
      <div className="flex items-center gap-2">
        <input
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder={selectedTicker || "TICKER"}
          className="bg-bg-primary border border-border rounded px-2 py-1 text-xs w-20 focus:outline-none focus:border-blue-primary uppercase"
        />
        <input
          value={quantity}
          onChange={(e) => setQuantity(e.target.value)}
          placeholder="Qty"
          type="number"
          min="0"
          step="1"
          className="bg-bg-primary border border-border rounded px-2 py-1 text-xs w-16 focus:outline-none focus:border-blue-primary"
        />
        <button
          onClick={() => handleTrade("buy")}
          disabled={loading || !activeTicker || !quantity}
          className="bg-green/20 text-green border border-green/30 rounded px-3 py-1 text-xs font-bold hover:bg-green/30 disabled:opacity-40 transition-colors"
        >
          BUY
        </button>
        <button
          onClick={() => handleTrade("sell")}
          disabled={loading || !activeTicker || !quantity}
          className="bg-red/20 text-red border border-red/30 rounded px-3 py-1 text-xs font-bold hover:bg-red/30 disabled:opacity-40 transition-colors"
        >
          SELL
        </button>
        {error && <span className="text-red text-xs ml-2">{error}</span>}
      </div>
    </div>
  );
}
