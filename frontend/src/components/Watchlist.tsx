"use client";

import { useState, type FormEvent } from "react";
import { useAppData } from "./AppDataProvider";
import { PriceCell } from "./PriceCell";
import { Sparkline } from "./Sparkline";
import { usePrice, usePriceHistory } from "@/lib/priceStore";
import { formatPercent, toneClass } from "@/lib/format";
import type { WatchlistItem } from "@/lib/types";

export function Watchlist() {
  const { watchlist, selected, select, addTicker, removeTicker } = useAppData();
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const onAdd = async (e: FormEvent) => {
    e.preventDefault();
    const ticker = input.trim().toUpperCase();
    if (!ticker) return;
    setBusy(true);
    setError(null);
    try {
      await addTicker(ticker);
      setInput("");
      select(ticker);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const onRemove = async (ticker: string) => {
    setError(null);
    try {
      await removeTicker(ticker);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <section data-testid="watchlist" className="panel flex min-h-0 flex-col" aria-label="Watchlist">
      <div className="panel-head">
        <h2 className="panel-title">Watchlist</h2>
        <span className="text-xs text-dim num">{watchlist.length}</span>
      </div>

      <form onSubmit={onAdd} className="flex gap-1.5 border-b border-line p-2">
        <input
          data-testid="watchlist-add-input"
          value={input}
          onChange={(e) => setInput(e.target.value.toUpperCase())}
          placeholder="Add ticker"
          aria-label="Ticker to add"
          maxLength={10}
          className="field min-w-0 flex-1 uppercase"
        />
        <button
          data-testid="watchlist-add-button"
          type="submit"
          disabled={busy || !input.trim()}
          className="btn btn-ghost"
        >
          Add
        </button>
      </form>
      {error && (
        <p data-testid="watchlist-error" role="alert" className="border-b border-line px-3 py-1.5 text-xs text-down">
          {error}
        </p>
      )}

      <div className="grid grid-cols-[1fr_auto_auto] gap-x-3 px-3 py-1.5 text-[11px] text-dim">
        <span>Symbol</span>
        <span className="text-right">Last</span>
        <span className="w-[52px] text-right">Day</span>
      </div>
      <ul className="min-h-0 flex-1 overflow-y-auto">
        {watchlist.map((item) => (
          <WatchlistRow
            key={item.ticker}
            item={item}
            active={item.ticker === selected}
            onSelect={() => select(item.ticker)}
            onRemove={() => onRemove(item.ticker)}
          />
        ))}
        {watchlist.length === 0 && (
          <li className="px-3 py-6 text-center text-xs text-muted">
            Your watchlist is empty. Add a ticker above to start streaming its price.
          </li>
        )}
      </ul>
    </section>
  );
}

export function WatchlistRow({
  item,
  active,
  onSelect,
  onRemove,
}: {
  item: WatchlistItem;
  active: boolean;
  onSelect: () => void;
  onRemove: () => void;
}) {
  const live = usePrice(item.ticker);
  const history = usePriceHistory(item.ticker);
  const price = live?.price ?? item.price;
  const day = live?.day_change_percent ?? item.day_change_percent;

  return (
    <li
      data-testid={`watchlist-row-${item.ticker}`}
      data-active={active || undefined}
      className="wl-row group"
    >
      <button
        type="button"
        onClick={onSelect}
        aria-pressed={active}
        className="grid w-full grid-cols-[1fr_auto_auto] items-center gap-x-3 px-3 py-1.5 text-left"
      >
        <span className="flex min-w-0 items-center gap-2">
          <span className="w-12 truncate font-semibold text-text">{item.ticker}</span>
          <Sparkline points={history} />
        </span>
        <PriceCell price={price} testId={`watchlist-price-${item.ticker}`} className="text-right text-sm" />
        <span className={`num w-[52px] text-right text-xs ${toneClass(day)}`}>{formatPercent(day)}</span>
      </button>
      <button
        type="button"
        data-testid={`watchlist-remove-${item.ticker}`}
        onClick={onRemove}
        aria-label={`Remove ${item.ticker} from watchlist`}
        title={`Remove ${item.ticker}`}
        className="wl-remove"
      >
        ×
      </button>
    </li>
  );
}
