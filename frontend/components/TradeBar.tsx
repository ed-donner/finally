"use client";

import { useState } from "react";
import { executeTrade } from "@/lib/api";
import type { PriceMap } from "@/lib/types";
import { formatPrice } from "@/lib/format";

interface TradeBarProps {
  prices: PriceMap;
  onTradeExecuted: () => void;
}

export default function TradeBar({ prices, onTradeExecuted }: TradeBarProps) {
  const [ticker, setTicker] = useState("");
  const [quantity, setQuantity] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [isError, setIsError] = useState(false);

  const currentPrice = prices[ticker.toUpperCase()]?.price;

  async function handleTrade(side: "buy" | "sell") {
    const t = ticker.trim().toUpperCase();
    const qty = parseFloat(quantity);
    if (!t || !qty || qty <= 0) {
      setStatus("Enter a valid ticker and quantity");
      setIsError(true);
      return;
    }

    try {
      setStatus(null);
      const result = await executeTrade({ ticker: t, quantity: qty, side });
      setStatus(`${side.toUpperCase()} ${result.quantity} ${result.ticker} @ ${formatPrice(result.price)}`);
      setIsError(false);
      setTicker("");
      setQuantity("");
      onTradeExecuted();
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Trade failed");
      setIsError(true);
    }
  }

  return (
    <div className="flex flex-col gap-2 px-3 py-2 border-t border-border bg-bg-panel">
      <div className="flex items-center gap-2">
        <input
          type="text"
          value={ticker}
          onChange={(e) => setTicker(e.target.value)}
          placeholder="Ticker"
          className="w-20 bg-bg-primary border border-border rounded px-2 py-1 text-xs text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent-blue uppercase"
        />
        <input
          type="number"
          value={quantity}
          onChange={(e) => setQuantity(e.target.value)}
          placeholder="Qty"
          min="0"
          step="1"
          className="w-20 bg-bg-primary border border-border rounded px-2 py-1 text-xs text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent-blue"
        />
        {currentPrice && (
          <span className="text-text-muted text-xs tabular-nums">
            @{formatPrice(currentPrice)}
          </span>
        )}
        <button
          onClick={() => handleTrade("buy")}
          className="px-3 py-1 rounded text-xs font-bold bg-gain hover:bg-gain/80 text-white transition-colors"
        >
          BUY
        </button>
        <button
          onClick={() => handleTrade("sell")}
          className="px-3 py-1 rounded text-xs font-bold bg-loss hover:bg-loss/80 text-white transition-colors"
        >
          SELL
        </button>
      </div>
      {status && (
        <div className={`text-xs ${isError ? "text-loss" : "text-gain"}`}>
          {status}
        </div>
      )}
    </div>
  );
}
