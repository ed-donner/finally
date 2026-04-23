"use client";

import { useState } from "react";

import { Panel } from "@/components/ui/panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { usd } from "@/lib/format";
import { usePortfolio } from "@/lib/store/portfolio";
import { usePrices } from "@/lib/store/prices";
import { useToast } from "@/lib/store/toast";
import { useWatchlist } from "@/lib/store/watchlist";

export function TradeBar() {
  const { selected } = useWatchlist();
  const { prices } = usePrices();
  const { refresh: refreshPortfolio } = usePortfolio();
  const { push } = useToast();

  const [ticker, setTicker] = useState("");
  const [quantity, setQuantity] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // If user hasn't typed a ticker, pre-fill from selection
  const effectiveTicker = (ticker.trim() || selected || "").toUpperCase();
  const live = effectiveTicker ? prices[effectiveTicker]?.price : undefined;
  const qtyNum = Number.parseFloat(quantity);
  const estCost = live && Number.isFinite(qtyNum) && qtyNum > 0 ? live * qtyNum : null;

  const submit = async (side: "buy" | "sell") => {
    if (!effectiveTicker || !Number.isFinite(qtyNum) || qtyNum <= 0) return;
    setSubmitting(true);
    try {
      const res = await api.trade({ ticker: effectiveTicker, side, quantity: qtyNum });
      push("ok", `${side.toUpperCase()} ${qtyNum} ${effectiveTicker} @ ${usd(res.price)}`);
      setQuantity("");
      await refreshPortfolio();
    } catch (err) {
      const detail = err instanceof Error ? err.message : "trade failed";
      push("error", detail);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Panel
      title="Order ticket"
      accent={effectiveTicker || undefined}
      actions={
        live && (
          <span className="text-xs text-muted tabular-nums">
            mkt {usd(live)}
          </span>
        )
      }
    >
      <div className="p-3 grid grid-cols-[1fr_120px_auto_auto] gap-2 items-end">
        <label className="block">
          <span className="block text-[9px] uppercase tracking-[0.22em] text-dim mb-1">Symbol</span>
          <Input
            value={ticker}
            placeholder={selected ?? "AAPL"}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            className="uppercase"
          />
        </label>
        <label className="block">
          <span className="block text-[9px] uppercase tracking-[0.22em] text-dim mb-1">Qty</span>
          <Input
            value={quantity}
            type="number"
            min={0}
            step="any"
            placeholder="0"
            onChange={(e) => setQuantity(e.target.value)}
          />
        </label>
        <Button
          variant="buy"
          disabled={submitting || !effectiveTicker || !(qtyNum > 0)}
          onClick={() => submit("buy")}
        >
          Buy
        </Button>
        <Button
          variant="sell"
          disabled={submitting || !effectiveTicker || !(qtyNum > 0)}
          onClick={() => submit("sell")}
        >
          Sell
        </Button>
      </div>
      <div className="px-3 pb-3 text-[10px] text-dim tabular-nums">
        {estCost !== null ? (
          <>
            Estimated notional <span className="text-ink">{usd(estCost)}</span>
            <span className="px-2 text-line-strong">|</span>
            Market order, instant fill
          </>
        ) : (
          <>Market order, instant fill at current price</>
        )}
      </div>
    </Panel>
  );
}
