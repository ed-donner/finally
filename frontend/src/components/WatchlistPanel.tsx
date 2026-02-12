import { useEffect, useState } from 'react';

import { Panel } from '@/src/components/Panel';
import { Sparkline } from '@/src/components/Sparkline';
import { money, pct } from '@/src/lib/format';
import { WatchlistItem } from '@/src/types/trading';

interface WatchlistPanelProps {
  watchlist: WatchlistItem[];
  sparklineByTicker: Record<string, number[]>;
  selectedTicker: string;
  onSelectTicker: (ticker: string) => void;
  onRemoveTicker: (ticker: string) => void;
}

export const WatchlistPanel = ({
  watchlist,
  sparklineByTicker,
  selectedTicker,
  onSelectTicker,
  onRemoveTicker,
}: WatchlistPanelProps) => {
  const [flashMap, setFlashMap] = useState<Record<string, 'up' | 'down' | 'flat'>>({});

  useEffect(() => {
    const entries = watchlist
      .filter((item) => item.direction !== 'flat')
      .map((item) => [item.ticker, item.direction] as const);
    if (!entries.length) return;

    setFlashMap((current) => ({ ...current, ...Object.fromEntries(entries) }));
    const timeout = setTimeout(() => {
      setFlashMap((current) => {
        const next = { ...current };
        for (const [ticker] of entries) {
          next[ticker] = 'flat';
        }
        return next;
      });
    }, 520);

    return () => clearTimeout(timeout);
  }, [watchlist]);

  return (
    <Panel title="Watchlist" className="h-full" testId="panel-watchlist">
      <div className="space-y-2">
        {watchlist.map((item) => {
          const isActive = selectedTicker === item.ticker;
          const flash = flashMap[item.ticker] ?? 'flat';
          const flashClass = flash === 'up' ? 'animate-pulseUp' : flash === 'down' ? 'animate-pulseDown' : '';

          return (
            <button
              key={item.ticker}
              type="button"
              data-testid={`watchlist-row-${item.ticker}`}
              onClick={() => onSelectTicker(item.ticker)}
              className={`grid w-full grid-cols-[72px_1fr_90px_26px] items-center gap-3 rounded border px-2 py-2 text-left transition ${
                isActive
                  ? 'border-terminal-blue bg-terminal-panelAlt/70'
                  : 'border-terminal-border bg-terminal-panelAlt/30 hover:border-terminal-blue/60'
              } ${flashClass}`}
            >
              <span className="font-semibold text-terminal-text">{item.ticker}</span>
              <Sparkline
                values={sparklineByTicker[item.ticker] ?? []}
                stroke={item.direction === 'down' ? '#f26478' : '#2ed68f'}
              />
              <span className={`text-right font-medium ${item.changePercent >= 0 ? 'text-terminal-positive' : 'text-terminal-negative'}`}>
                {money(item.price)}
                <span className="block text-xs">{pct(item.changePercent)}</span>
              </span>
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  onRemoveTicker(item.ticker);
                }}
                className="cursor-pointer text-center text-xs text-terminal-dim hover:text-terminal-negative"
                aria-label={`remove-${item.ticker}`}
                data-testid={`watchlist-remove-${item.ticker}`}
              >
                x
              </button>
            </button>
          );
        })}
      </div>
    </Panel>
  );
};
