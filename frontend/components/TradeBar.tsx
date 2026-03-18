"use client";

import { useState } from "react";
import { useStore } from "@/lib/store";
import { executeTrade } from "@/lib/api";

export function TradeBar() {
  const selectedTicker = useStore((s) => s.selectedTicker);
  const setPortfolio = useStore((s) => s.setPortfolio);
  const [ticker, setTicker] = useState("");
  const [quantity, setQuantity] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const activeTicker = ticker || selectedTicker || "";

  const handleTrade = async (side: "buy" | "sell") => {
    const t = activeTicker.trim().toUpperCase();
    const qty = parseFloat(quantity);
    if (!t || !qty || qty <= 0) {
      setStatus("Enter a valid ticker and quantity");
      return;
    }

    setIsSubmitting(true);
    setStatus(null);
    try {
      const result = await executeTrade({ ticker: t, quantity: qty, side });
      setPortfolio(result.portfolio);
      setStatus(
        `${side.toUpperCase()} ${qty} ${t} @ $${result.trade.price.toFixed(2)}`
      );
      setQuantity("");
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Trade failed");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="flex items-center gap-2 px-3 py-2 bg-bg-secondary border-t border-border">
      <input
        value={ticker}
        onChange={(e) => setTicker(e.target.value.toUpperCase())}
        placeholder={selectedTicker || "TICKER"}
        maxLength={5}
        className="w-20 bg-bg-input text-xs text-text-primary px-2 py-1.5 rounded border border-border focus:border-accent-blue outline-none"
      />
      <input
        value={quantity}
        onChange={(e) => setQuantity(e.target.value)}
        placeholder="Qty"
        type="number"
        min="0"
        step="1"
        className="w-20 bg-bg-input text-xs text-text-primary px-2 py-1.5 rounded border border-border focus:border-accent-blue outline-none"
      />
      <button
        onClick={() => handleTrade("buy")}
        disabled={isSubmitting}
        className="px-3 py-1.5 text-xs font-bold bg-gain/20 text-gain border border-gain/30 rounded hover:bg-gain/30 transition-colors disabled:opacity-50"
      >
        BUY
      </button>
      <button
        onClick={() => handleTrade("sell")}
        disabled={isSubmitting}
        className="px-3 py-1.5 text-xs font-bold bg-loss/20 text-loss border border-loss/30 rounded hover:bg-loss/30 transition-colors disabled:opacity-50"
      >
        SELL
      </button>
      {status && (
        <span className="text-xs text-text-secondary ml-2 truncate">
          {status}
        </span>
      )}
    </div>
  );
}
