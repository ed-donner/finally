"use client";

import { useEffect, useState } from "react";
import { useAppData } from "./AppDataProvider";
import { api } from "@/lib/api";
import { usePrice } from "@/lib/priceStore";
import { formatMoney, formatPrice, formatQuantity } from "@/lib/format";

export function TradeBar() {
  const { selected, setPortfolio, refreshHistory, refreshWatchlist } = useAppData();
  const [ticker, setTicker] = useState(selected ?? "");
  const [quantity, setQuantity] = useState("");
  const [busy, setBusy] = useState<"buy" | "sell" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const live = usePrice(ticker);

  useEffect(() => {
    if (selected) setTicker(selected);
  }, [selected]);

  const qty = Number(quantity);
  const valid = ticker.trim() !== "" && Number.isFinite(qty) && qty > 0;
  const estimate = valid && live ? qty * live.price : null;

  const submit = async (side: "buy" | "sell") => {
    const sym = ticker.trim().toUpperCase();
    if (!sym) return setError("Enter a ticker symbol.");
    if (!Number.isFinite(qty) || qty <= 0) return setError("Enter a quantity greater than zero.");
    setBusy(side);
    setError(null);
    setResult(null);
    try {
      const res = await api.trade(sym, qty, side);
      setPortfolio(res.portfolio);
      setResult(
        `${side === "buy" ? "Bought" : "Sold"} ${formatQuantity(res.trade.quantity)} ${res.trade.ticker} at ${formatMoney(res.trade.price)}`,
      );
      setQuantity("");
      void refreshHistory();
      void refreshWatchlist();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="panel flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2" aria-label="Trade">
      <h2 className="panel-title mr-1">Trade</h2>
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void submit("buy");
        }}
      >
        <input
          data-testid="trade-ticker-input"
          aria-label="Ticker"
          placeholder="Ticker"
          value={ticker}
          maxLength={10}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          className="field w-24 uppercase"
        />
        <input
          data-testid="trade-quantity-input"
          aria-label="Quantity"
          placeholder="Qty"
          type="number"
          min="0"
          step="any"
          inputMode="decimal"
          value={quantity}
          onChange={(e) => setQuantity(e.target.value)}
          className="field num w-24"
        />
        <button
          data-testid="trade-buy-button"
          type="button"
          disabled={busy !== null}
          onClick={() => submit("buy")}
          className="btn btn-submit"
        >
          {busy === "buy" ? "Buying…" : "Buy"}
        </button>
        <button
          data-testid="trade-sell-button"
          type="button"
          disabled={busy !== null}
          onClick={() => submit("sell")}
          className="btn btn-submit-outline"
        >
          {busy === "sell" ? "Selling…" : "Sell"}
        </button>
      </form>
      <div className="flex min-w-0 flex-1 items-center justify-end gap-3 text-xs">
        {error ? (
          <span data-testid="trade-error" role="alert" className="text-down">
            {error}
          </span>
        ) : result ? (
          <span data-testid="trade-success" role="status" className="text-up">
            {result}
          </span>
        ) : (
          <span className="num text-muted">
            {live ? `${ticker} ${formatPrice(live.price)}` : "Market order, fills instantly"}
            {estimate != null && ` · est. ${formatMoney(estimate)}`}
          </span>
        )}
      </div>
    </section>
  );
}
