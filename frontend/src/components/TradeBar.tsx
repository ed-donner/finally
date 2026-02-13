import { FormEvent, useState } from 'react';

import { Panel } from '@/src/components/Panel';
import { toTicker } from '@/src/lib/format';

interface TradeBarProps {
  defaultTicker: string;
  onTrade: (payload: { ticker: string; quantity: number; side: 'buy' | 'sell' }) => Promise<void>;
  onAddTicker: (ticker: string) => Promise<void>;
}

export const TradeBar = ({ defaultTicker, onTrade, onAddTicker }: TradeBarProps) => {
  const [ticker, setTicker] = useState(defaultTicker);
  const [quantity, setQuantity] = useState('1');
  const [isBusy, setBusy] = useState(false);

  const runTrade = async (side: 'buy' | 'sell') => {
    const normalizedTicker = toTicker(ticker);
    const parsedQty = Number(quantity);
    if (!normalizedTicker || parsedQty <= 0) return;

    setBusy(true);
    try {
      await onTrade({ ticker: normalizedTicker, quantity: parsedQty, side });
    } finally {
      setBusy(false);
    }
  };

  const addTicker = async (event: FormEvent) => {
    event.preventDefault();
    const normalized = toTicker(ticker);
    if (!normalized) return;

    setBusy(true);
    try {
      await onAddTicker(normalized);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title="Trade Bar" className="h-full" testId="panel-trade-bar">
      <form data-testid="trade-form" onSubmit={addTicker} className="grid grid-cols-2 gap-2">
        <input
          data-testid="trade-ticker-input"
          aria-label="Trade ticker"
          value={ticker}
          onChange={(event) => setTicker(event.target.value.toUpperCase())}
          placeholder="Ticker"
          maxLength={5}
          className="min-w-0 rounded border border-terminal-border bg-terminal-bg px-2 py-2 text-sm text-terminal-text outline-none focus:border-terminal-blue"
        />
        <input
          data-testid="trade-quantity-input"
          aria-label="Trade quantity"
          value={quantity}
          onChange={(event) => setQuantity(event.target.value)}
          type="number"
          min="0"
          step="0.01"
          placeholder="Quantity"
          className="min-w-0 rounded border border-terminal-border bg-terminal-bg px-2 py-2 text-sm text-terminal-text outline-none focus:border-terminal-blue"
        />
        <button
          data-testid="trade-buy-button"
          type="button"
          onClick={() => void runTrade('buy')}
          disabled={isBusy}
          className="min-w-0 rounded bg-terminal-positive px-2 py-2 text-sm font-semibold text-terminal-bg disabled:opacity-60"
        >
          Buy
        </button>
        <button
          data-testid="trade-sell-button"
          type="button"
          onClick={() => void runTrade('sell')}
          disabled={isBusy}
          className="min-w-0 rounded bg-terminal-negative px-2 py-2 text-sm font-semibold text-terminal-bg disabled:opacity-60"
        >
          Sell
        </button>
        <button
          data-testid="trade-add-watchlist-button"
          type="submit"
          disabled={isBusy}
          className="col-span-2 min-w-0 rounded bg-terminal-violet px-2 py-2 text-sm font-semibold text-white disabled:opacity-60"
        >
          Watch
        </button>
      </form>
    </Panel>
  );
};
