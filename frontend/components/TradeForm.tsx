"use client";

import { useEffect, useState } from "react";
import { fmtUsd, TICKER_RE } from "@/utils/format";
import type { Side, TradeOrder } from "@/utils/types";
import { Panel } from "./Panel";

interface TradeFormProps {
  /** Ticker selected elsewhere (watchlist / positions); prefills the field. */
  selectedTicker: string | null;
  /** Latest price for a ticker, for the cost estimate. */
  priceOf: (ticker: string) => number | undefined;
  /** Resolves when the trade finished; the parent shows the success/error toast. */
  onTrade: (order: TradeOrder) => Promise<boolean>;
}

/** Market-order form: ticker + quantity + Buy / Sell. No confirmation dialog (PLAN.md section 2). */
export function TradeForm({ selectedTicker, priceOf, onTrade }: TradeFormProps) {
  const [ticker, setTicker] = useState("");
  const [quantity, setQuantity] = useState("");
  const [pending, setPending] = useState<Side | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    if (selectedTicker) setTicker(selectedTicker);
  }, [selectedTicker]);

  const qty = Number(quantity);
  const price = priceOf(ticker.trim().toUpperCase());
  const estimate = price && qty > 0 ? price * qty : null;

  async function submit(side: Side) {
    const t = ticker.trim().toUpperCase();
    if (!TICKER_RE.test(t)) return setFormError("Enter a valid ticker, e.g. AAPL");
    if (!(qty > 0)) return setFormError("Quantity must be greater than zero");
    setFormError(null);
    setPending(side);
    try {
      const ok = await onTrade({ ticker: t, side, quantity: qty });
      if (ok) setQuantity(""); // keep the ticker so the user can trade it again quickly
    } finally {
      setPending(null);
    }
  }

  const busy = pending !== null;

  return (
    <Panel title="Trade" bodyClassName="p-3">
      <form onSubmit={(e) => e.preventDefault()} className="flex flex-col gap-2">
        <div className="grid grid-cols-2 gap-2">
          <label className="flex flex-col gap-1 text-[10px] uppercase tracking-wider text-muted">
            Ticker
            <input
              value={ticker}
              onChange={(e) => setTicker(e.target.value.toUpperCase())}
              placeholder="AAPL"
              maxLength={7}
              disabled={busy}
              className="rounded border border-line bg-canvas px-2 py-1.5 text-sm normal-case tracking-normal text-white outline-none focus:border-primary"
            />
          </label>
          <label className="flex flex-col gap-1 text-[10px] uppercase tracking-wider text-muted">
            Quantity
            <input
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              placeholder="0"
              inputMode="decimal"
              disabled={busy}
              className="num rounded border border-line bg-canvas px-2 py-1.5 text-sm normal-case tracking-normal text-white outline-none focus:border-primary"
            />
          </label>
        </div>

        <p className="num h-4 text-[11px] text-muted">
          {price ? `Market ${fmtUsd(price)}` : ""}
          {estimate ? ` · est. ${fmtUsd(estimate)}` : ""}
        </p>
        {formError && <p className="text-[11px] text-down">{formError}</p>}

        <div className="grid grid-cols-2 gap-2">
          <button
            type="button"
            onClick={() => void submit("buy")}
            disabled={busy}
            className="rounded bg-secondary py-1.5 text-sm font-semibold text-white hover:brightness-125 disabled:opacity-50"
          >
            {pending === "buy" ? "Buying…" : "Buy"}
          </button>
          <button
            type="button"
            onClick={() => void submit("sell")}
            disabled={busy}
            className="rounded border border-secondary py-1.5 text-sm font-semibold text-secondary hover:bg-secondary/15 disabled:opacity-50"
          >
            {pending === "sell" ? "Selling…" : "Sell"}
          </button>
        </div>
      </form>
    </Panel>
  );
}
