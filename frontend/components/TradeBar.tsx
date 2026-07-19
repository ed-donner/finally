'use client';

import { useEffect, useState } from 'react';

interface TradeBarProps {
  /** Currently selected ticker — prefills the ticker field. */
  selected: string | null;
  /** Executes a market order; rejects (throws) on validation failure. */
  onTrade: (ticker: string, quantity: number, side: 'buy' | 'sell') => Promise<void>;
}

/** Market-order entry: ticker + quantity, instant Buy/Sell, no confirmation. */
export default function TradeBar({ selected, onTrade }: TradeBarProps) {
  const [ticker, setTicker] = useState('');
  const [qty, setQty] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (selected) setTicker(selected);
  }, [selected]);

  const submit = async (side: 'buy' | 'sell') => {
    const t = ticker.trim().toUpperCase();
    const q = Number.parseFloat(qty);
    if (!t || !Number.isFinite(q) || q <= 0) return;
    setBusy(true);
    try {
      await onTrade(t, q, side);
      setQty('');
    } catch {
      // Error surfaced by the parent's toast; keep the form intact for a retry.
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel shrink-0" data-testid="trade-bar">
      <div className="panel-title">
        <span>Trade</span>
        <span className="text-terminal-muted/60 normal-case tracking-normal">market order · instant fill</span>
      </div>
      <div className="flex items-center gap-2 p-2">
        <input
          value={ticker}
          onChange={(e) => setTicker(e.target.value)}
          placeholder="Ticker"
          aria-label="Trade ticker"
          data-testid="trade-ticker"
          className="field w-24 uppercase"
          maxLength={8}
        />
        <input
          value={qty}
          onChange={(e) => setQty(e.target.value)}
          placeholder="Qty"
          aria-label="Trade quantity"
          data-testid="trade-quantity"
          type="number"
          min="0"
          step="any"
          className="field w-24 num"
        />
        <div className="flex-1" />
        <button
          type="button"
          className="btn-buy"
          data-testid="trade-buy"
          disabled={busy}
          onClick={() => submit('buy')}
        >
          Buy
        </button>
        <button
          type="button"
          className="btn-sell"
          data-testid="trade-sell"
          disabled={busy}
          onClick={() => submit('sell')}
        >
          Sell
        </button>
      </div>
    </div>
  );
}
