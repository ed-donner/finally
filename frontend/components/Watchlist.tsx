"use client";

import { memo, useState } from "react";
import { useFlash } from "@/hooks/useFlash";
import { fmtPct, fmtUsd, signClass, TICKER_RE } from "@/utils/format";
import type { PricePoint, PriceTick, WatchlistEntry } from "@/utils/types";
import { Panel, PanelMessage } from "./Panel";
import { Sparkline } from "./Sparkline";

interface WatchlistProps {
  entries: WatchlistEntry[] | null;
  loading: boolean;
  error: string | null;
  prices: Record<string, PriceTick>;
  history: Record<string, PricePoint[]>;
  open: Record<string, number>;
  selected: string | null;
  onSelect: (ticker: string) => void;
  onAdd: (ticker: string) => Promise<void>;
  onRemove: (ticker: string) => Promise<void>;
}

export function Watchlist({ entries, loading, error, prices, history, open, selected, onSelect, onAdd, onRemove }: WatchlistProps) {
  const [input, setInput] = useState("");
  const [adding, setAdding] = useState(false);
  const [inputError, setInputError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const ticker = input.trim().toUpperCase();
    if (!TICKER_RE.test(ticker)) return setInputError("Enter a ticker like AAPL");
    setInputError(null);
    setAdding(true);
    try {
      await onAdd(ticker);
      setInput("");
    } finally {
      setAdding(false);
    }
  }

  return (
    <Panel title="Watchlist" right={<span className="text-[11px] text-muted">{entries?.length ?? 0} tickers</span>} bodyClassName="flex flex-col">
      <form onSubmit={submit} className="flex gap-2 border-b border-line p-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value.toUpperCase())}
          placeholder="Add ticker…"
          maxLength={7}
          aria-label="Add ticker to watchlist"
          className="min-w-0 flex-1 rounded border border-line bg-canvas px-2 py-1 text-xs uppercase outline-none focus:border-primary"
        />
        <button
          disabled={adding || !input}
          className="rounded bg-primary/15 px-3 text-xs font-semibold text-primary hover:bg-primary/25 disabled:opacity-40"
        >
          {adding ? "…" : "Add"}
        </button>
      </form>
      {inputError && <p className="px-3 pt-1 text-[11px] text-down">{inputError}</p>}

      {loading ? (
        <PanelMessage>Loading watchlist…</PanelMessage>
      ) : error && !entries ? (
        <PanelMessage>
          <span className="text-down">{error}</span>
        </PanelMessage>
      ) : !entries?.length ? (
        <PanelMessage>Nothing watched yet. Add a ticker above.</PanelMessage>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <table className="w-full">
            <thead className="sticky top-0 bg-panel text-[10px] uppercase tracking-wider text-muted">
              <tr>
                <th className="px-3 py-1.5 text-left font-medium">Symbol</th>
                <th className="px-2 py-1.5 text-right font-medium">Price</th>
                <th className="px-2 py-1.5 text-right font-medium" title="Change since the first price seen this session">
                  Chg %
                </th>
                <th className="px-2 py-1.5 text-left font-medium">Trend</th>
                <th className="w-6" />
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <WatchRow
                  key={entry.ticker}
                  entry={entry}
                  tick={prices[entry.ticker]}
                  points={history[entry.ticker]}
                  openPrice={open[entry.ticker]}
                  selected={entry.ticker === selected}
                  onSelect={onSelect}
                  onRemove={onRemove}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

interface WatchRowProps {
  entry: WatchlistEntry;
  tick: PriceTick | undefined;
  points: PricePoint[] | undefined;
  openPrice: number | undefined;
  selected: boolean;
  onSelect: (ticker: string) => void;
  onRemove: (ticker: string) => Promise<void>;
}

const WatchRow = memo(function WatchRow({ entry, tick, points, openPrice, selected, onSelect, onRemove }: WatchRowProps) {
  const price = tick?.price ?? entry.price;
  const { className: flash, flashKey } = useFlash(price);
  const change = price != null && openPrice ? ((price - openPrice) / openPrice) * 100 : null;

  return (
    <tr
      data-testid={`watch-row-${entry.ticker}`}
      onClick={() => onSelect(entry.ticker)}
      className={`cursor-pointer border-b border-line/40 hover:bg-raised ${selected ? "bg-raised shadow-[inset_2px_0_0_#ecad0a]" : ""}`}
    >
      <td className="px-3 py-1.5 font-semibold text-white">{entry.ticker}</td>
      {/* Keyed span: the key changes on each flash so the 500ms animation restarts every tick. */}
      <td className="px-2 py-1.5 text-right">
        <span data-testid={`watch-price-${entry.ticker}`} key={flashKey} className={`num inline-block rounded px-1 ${flash}`}>
          {fmtUsd(price)}
        </span>
      </td>
      <td className={`num px-2 py-1.5 text-right ${signClass(change)}`}>{fmtPct(change)}</td>
      <td className="px-2 py-1">
        <Sparkline points={points ?? []} />
      </td>
      <td className="pr-2 text-right">
        <button
          onClick={(e) => {
            e.stopPropagation();
            void onRemove(entry.ticker);
          }}
          aria-label={`Remove ${entry.ticker}`}
          className="text-muted hover:text-down"
        >
          ×
        </button>
      </td>
    </tr>
  );
});
