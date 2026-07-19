'use client';

import { useEffect, useRef, useState } from 'react';
import type { PricePoint, WatchlistItem } from '@/lib/types';
import { formatPercent, formatPrice } from '@/lib/format';
import Sparkline from './Sparkline';

interface WatchlistProps {
  items: WatchlistItem[];
  history: Record<string, PricePoint[]>;
  selected: string | null;
  onSelect: (ticker: string) => void;
  onRemove: (ticker: string) => void;
  onAdd: (ticker: string) => void;
}

/** A price cell that briefly flashes green/red when its value changes. */
function PriceCell({ ticker, price }: { ticker: string; price: number | null }) {
  const prev = useRef<number | null>(null);
  const [flash, setFlash] = useState<'up' | 'down' | null>(null);

  useEffect(() => {
    if (price === null) return;
    if (prev.current !== null && price !== prev.current) {
      setFlash(price > prev.current ? 'up' : 'down');
      const id = setTimeout(() => setFlash(null), 500);
      prev.current = price;
      return () => clearTimeout(id);
    }
    prev.current = price;
  }, [price]);

  const flashClass = flash === 'up' ? 'animate-flashUp' : flash === 'down' ? 'animate-flashDown' : '';

  return (
    <span
      data-testid="watchlist-price"
      data-ticker={ticker}
      className={`num tabular-nums rounded px-1 ${flashClass}`}
    >
      {formatPrice(price)}
    </span>
  );
}

export default function Watchlist({
  items,
  history,
  selected,
  onSelect,
  onRemove,
  onAdd,
}: WatchlistProps) {
  const [newTicker, setNewTicker] = useState('');

  const submitAdd = (e: React.FormEvent) => {
    e.preventDefault();
    const t = newTicker.trim().toUpperCase();
    if (t) {
      onAdd(t);
      setNewTicker('');
    }
  };

  return (
    <div className="panel h-full" data-testid="watchlist">
      <div className="panel-title">
        <span>Watchlist</span>
        <span className="text-terminal-muted/60">{items.length}</span>
      </div>

      <div className="flex-1 overflow-y-auto">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-terminal-panel z-10">
            <tr className="text-[10px] uppercase tracking-wider text-terminal-muted">
              <th className="text-left font-normal px-3 py-1.5">Symbol</th>
              <th className="text-right font-normal px-2 py-1.5">Last</th>
              <th className="text-right font-normal px-2 py-1.5">Chg%</th>
              <th className="text-center font-normal px-2 py-1.5">Trend</th>
              <th className="w-6" />
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const isSelected = item.ticker === selected;
              const changeColor =
                item.change_percent == null || item.change_percent === 0
                  ? 'text-terminal-muted'
                  : item.change_percent > 0
                    ? 'text-up'
                    : 'text-down';
              return (
                <tr
                  key={item.ticker}
                  data-testid="watchlist-row"
                  data-ticker={item.ticker}
                  onClick={() => onSelect(item.ticker)}
                  className={`group cursor-pointer border-b border-terminal-border/40 hover:bg-terminal-raised/60 ${
                    isSelected ? 'bg-terminal-raised' : ''
                  }`}
                >
                  <td className="px-3 py-1.5">
                    <span className="flex items-center gap-2">
                      <span
                        className={`w-0.5 h-4 rounded ${isSelected ? 'bg-accent' : 'bg-transparent'}`}
                      />
                      <span className="font-semibold text-terminal-text">{item.ticker}</span>
                    </span>
                  </td>
                  <td className="px-2 py-1.5 text-right">
                    <PriceCell ticker={item.ticker} price={item.price} />
                  </td>
                  <td className={`px-2 py-1.5 text-right num ${changeColor}`} data-testid="watchlist-change" data-ticker={item.ticker}>
                    {formatPercent(item.change_percent)}
                  </td>
                  <td className="px-2 py-1.5">
                    <div className="flex justify-center">
                      <Sparkline data={history[item.ticker] ?? []} />
                    </div>
                  </td>
                  <td className="pr-2">
                    <button
                      type="button"
                      aria-label={`Remove ${item.ticker}`}
                      data-testid="watchlist-remove"
                      data-ticker={item.ticker}
                      onClick={(e) => {
                        e.stopPropagation();
                        onRemove(item.ticker);
                      }}
                      className="opacity-0 group-hover:opacity-100 text-terminal-muted hover:text-down transition-opacity text-xs px-1"
                    >
                      ✕
                    </button>
                  </td>
                </tr>
              );
            })}
            {items.length === 0 && (
              <tr>
                <td colSpan={5} className="text-center text-terminal-muted py-6 text-xs">
                  Watchlist empty — add a ticker below.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <form onSubmit={submitAdd} className="flex gap-2 p-2 border-t border-terminal-border shrink-0">
        <input
          value={newTicker}
          onChange={(e) => setNewTicker(e.target.value)}
          placeholder="Add ticker…"
          aria-label="Add ticker to watchlist"
          data-testid="watchlist-add-input"
          className="field flex-1 uppercase"
          maxLength={8}
        />
        <button type="submit" className="btn-submit" data-testid="watchlist-add-button">
          Add
        </button>
      </form>
    </div>
  );
}
