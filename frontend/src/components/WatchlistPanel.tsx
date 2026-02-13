import { memo, useEffect, useMemo, useState } from 'react';

import { Panel } from '@/src/components/Panel';
import { Sparkline } from '@/src/components/Sparkline';
import { money, pct } from '@/src/lib/format';
import { WatchlistItem } from '@/src/types/trading';

interface WatchlistPanelProps {
  watchlist: WatchlistItem[];
  tickerHistory: Record<string, number[]>;
  selectedTicker: string;
  onSelectTicker: (ticker: string) => void;
  onRemoveTicker: (ticker: string) => void;
}

const DESKTOP_COLUMNS = 5;
const ROWS_PER_COLUMN = 10;
const PREFERRED_GROUP_ORDER = [
  'Tech',
  'Financials',
  'Healthcare',
  'Consumer',
  'Industrials & Energy',
];

interface WatchlistColumn {
  label: string;
  rows: Array<WatchlistItem | null>;
}

const buildColumns = (watchlist: WatchlistItem[]): WatchlistColumn[] => {
  const grouped = new Map<string, WatchlistItem[]>();
  for (const item of watchlist) {
    const key = item.group || 'Other';
    const items = grouped.get(key) ?? [];
    items.push(item);
    grouped.set(key, items);
  }

  const columns: WatchlistColumn[] = PREFERRED_GROUP_ORDER.map((label) => ({
    label,
    rows: [...(grouped.get(label) ?? [])],
  }));

  const overflow = [...grouped.entries()]
    .filter(([label]) => !PREFERRED_GROUP_ORDER.includes(label))
    .flatMap(([, items]) => items);
  if (overflow.length) {
    columns[DESKTOP_COLUMNS - 1].rows.push(...overflow);
  }

  for (const column of columns) {
    if (column.rows.length < ROWS_PER_COLUMN) {
      while (column.rows.length < ROWS_PER_COLUMN) column.rows.push(null);
    }
  }

  return columns;
};

export const WatchlistPanel = ({
  watchlist,
  tickerHistory,
  selectedTicker,
  onSelectTicker,
  onRemoveTicker,
}: WatchlistPanelProps) => {
  const [flashMap, setFlashMap] = useState<Record<string, 'up' | 'down' | 'flat'>>({});
  const columns = buildColumns(watchlist);

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

  const miniSeries = useMemo(() => {
    const next: Record<string, number[]> = {};
    for (const item of watchlist) {
      const history = tickerHistory[item.ticker] ?? [];
      if (history.length > 0) {
        next[item.ticker] = history.length > 32 ? history.slice(history.length - 32) : history;
        continue;
      }
      const fallback = item.price || item.dayBaselinePrice || item.previousPrice;
      next[item.ticker] = fallback > 0 ? [fallback] : [];
    }
    return next;
  }, [tickerHistory, watchlist]);

  return (
    <Panel title="Watchlist" className="h-full overflow-hidden" testId="panel-watchlist">
      <div
        data-testid="watchlist-grid"
        className="grid max-h-[calc(100vh-180px)] grid-cols-1 gap-1 overflow-y-auto overflow-x-hidden pr-1 md:grid-cols-2 xl:grid-cols-5"
      >
        {columns.map((column) => (
          <div key={column.label} data-testid={`watchlist-column-${column.label}`} className="space-y-1.5">
            <h3 className="px-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-terminal-blue/90">{column.label}</h3>
            {column.rows.map((item, index) => {
              if (!item) {
                return (
                  <div
                    key={`${column.label}-${index}`}
                    className="h-[52px] rounded border border-dashed border-terminal-border/45 bg-terminal-panelAlt/10"
                    aria-hidden="true"
                  />
                );
              }

              const isActive = selectedTicker === item.ticker;
              const flash = flashMap[item.ticker] ?? 'flat';
              const flashClass = flash === 'up' ? 'animate-pulseUp' : flash === 'down' ? 'animate-pulseDown' : '';

              return (
                <WatchlistRow
                  key={item.ticker}
                  item={item}
                  series={miniSeries[item.ticker] ?? []}
                  isActive={isActive}
                  flashClass={flashClass}
                  onSelectTicker={onSelectTicker}
                  onRemoveTicker={onRemoveTicker}
                />
              );
            })}
          </div>
        ))}
      </div>
    </Panel>
  );
};

interface WatchlistRowProps {
  item: WatchlistItem;
  series: number[];
  isActive: boolean;
  flashClass: string;
  onSelectTicker: (ticker: string) => void;
  onRemoveTicker: (ticker: string) => void;
}

const WatchlistRow = memo(({
  item,
  series,
  isActive,
  flashClass,
  onSelectTicker,
  onRemoveTicker,
}: WatchlistRowProps) => (
  <div
    role="button"
    tabIndex={0}
    data-testid={`watchlist-row-${item.ticker}`}
    onClick={() => onSelectTicker(item.ticker)}
    onKeyDown={(event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        onSelectTicker(item.ticker);
      }
    }}
    className={`grid h-[52px] w-full min-w-0 cursor-pointer grid-cols-[1fr_56px_16px] items-center gap-2 rounded border px-2 text-left transition ${
      isActive
        ? 'border-terminal-blue bg-terminal-panelAlt/70'
        : 'border-terminal-border bg-terminal-panelAlt/30 hover:border-terminal-blue/60'
    } ${flashClass}`}
  >
    <div className="min-w-0">
      <span className="block truncate text-xs font-semibold text-terminal-text">{item.ticker}</span>
      <Sparkline
        values={series}
        stroke={item.changePercent >= 0 ? '#2cc57f' : '#eb5d5d'}
        height={14}
        className="mt-1 h-[14px]"
      />
    </div>
    <span className={`text-right text-[10px] font-medium ${item.changePercent >= 0 ? 'text-terminal-positive' : 'text-terminal-negative'}`}>
      <span className="block">{money(item.price)}</span>
      <span className="block">{pct(item.changePercent)}</span>
    </span>
    <button
      type="button"
      onClick={(event) => {
        event.stopPropagation();
        onRemoveTicker(item.ticker);
      }}
      className="cursor-pointer text-center text-[10px] text-terminal-dim hover:text-terminal-negative"
      aria-label={`remove-${item.ticker}`}
      data-testid={`watchlist-remove-${item.ticker}`}
    >
      x
    </button>
  </div>
), (prev, next) => (
  prev.item === next.item
  && prev.series === next.series
  && prev.isActive === next.isActive
  && prev.flashClass === next.flashClass
));
