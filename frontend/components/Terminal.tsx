'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError } from '@/lib/api';
import { overlayLivePrices } from '@/lib/portfolio';
import type { ChatMessage, Portfolio, Snapshot, WatchlistItem } from '@/lib/types';
import { usePriceStream } from '@/lib/usePriceStream';
import ChatPanel from './ChatPanel';
import Header from './Header';
import MainChart from './MainChart';
import PnLChart from './PnLChart';
import PortfolioHeatmap from './PortfolioHeatmap';
import PositionsTable from './PositionsTable';
import TradeBar from './TradeBar';
import Watchlist from './Watchlist';

const POLL_MS = 5000;

export default function Terminal() {
  const { prices, history, status } = usePriceStream();

  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [watchTickers, setWatchTickers] = useState<string[]>([]);
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chatLoading, setChatLoading] = useState(false);
  const [chatCollapsed, setChatCollapsed] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const flash = useCallback((msg: string) => {
    setToast(msg);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 4000);
  }, []);

  const refreshPortfolio = useCallback(async () => {
    try {
      setPortfolio(await api.getPortfolio());
    } catch {
      /* transient; the poll will retry */
    }
  }, []);

  const refreshWatchlist = useCallback(async () => {
    try {
      const { watchlist } = await api.getWatchlist();
      setWatchTickers(watchlist.map((w) => w.ticker));
    } catch {
      /* transient */
    }
  }, []);

  const refreshHistory = useCallback(async () => {
    try {
      const { snapshots } = await api.getHistory();
      setSnapshots(snapshots);
    } catch {
      /* transient */
    }
  }, []);

  // Initial load + periodic reconciliation with the server.
  useEffect(() => {
    refreshPortfolio();
    refreshWatchlist();
    refreshHistory();
    const id = setInterval(() => {
      refreshPortfolio();
      refreshHistory();
    }, POLL_MS);
    return () => clearInterval(id);
  }, [refreshPortfolio, refreshWatchlist, refreshHistory]);

  // Default the selected ticker once the watchlist arrives.
  useEffect(() => {
    if (!selected && watchTickers.length) setSelected(watchTickers[0]);
  }, [watchTickers, selected]);

  // Overlay live stream prices for a real-time portfolio valuation.
  const livePortfolio = useMemo(
    () => (portfolio ? overlayLivePrices(portfolio, prices) : null),
    [portfolio, prices],
  );

  // Watchlist rows: server ticker order, live prices merged in.
  const watchlistItems: WatchlistItem[] = useMemo(
    () =>
      watchTickers.map((ticker) => {
        const u = prices[ticker];
        return u
          ? {
              ticker,
              price: u.price,
              previous_price: u.previous_price,
              change: u.change,
              change_percent: u.change_percent,
              direction: u.direction,
            }
          : {
              ticker,
              price: null,
              previous_price: null,
              change: null,
              change_percent: null,
              direction: null,
            };
      }),
    [watchTickers, prices],
  );

  const handleTrade = useCallback(
    async (ticker: string, quantity: number, side: 'buy' | 'sell') => {
      try {
        const res = await api.trade({ ticker, quantity, side });
        setPortfolio(res.portfolio);
        refreshHistory();
        flash(`${side === 'buy' ? 'Bought' : 'Sold'} ${quantity} ${ticker} @ ${res.trade.price?.toFixed(2) ?? '—'}`);
      } catch (e) {
        flash(e instanceof ApiError ? e.message : 'Trade failed.');
        throw e;
      }
    },
    [flash, refreshHistory],
  );

  const handleAdd = useCallback(
    async (ticker: string) => {
      try {
        const { watchlist } = await api.addWatchlist(ticker);
        setWatchTickers(watchlist.map((w) => w.ticker));
      } catch (e) {
        flash(e instanceof ApiError ? e.message : 'Could not add ticker.');
      }
    },
    [flash],
  );

  const handleRemove = useCallback(
    async (ticker: string) => {
      try {
        const { watchlist } = await api.removeWatchlist(ticker);
        const tickers = watchlist.map((w) => w.ticker);
        setWatchTickers(tickers);
        setSelected((cur) => (cur === ticker ? tickers[0] ?? null : cur));
      } catch (e) {
        flash(e instanceof ApiError ? e.message : 'Could not remove ticker.');
      }
    },
    [flash],
  );

  const handleSend = useCallback(
    async (text: string) => {
      setMessages((m) => [...m, { role: 'user', content: text }]);
      setChatLoading(true);
      try {
        const res = await api.chat(text);
        setMessages((m) => [
          ...m,
          {
            role: 'assistant',
            content: res.message,
            trades: res.trades,
            watchlist_changes: res.watchlist_changes,
          },
        ]);
        if (res.portfolio) setPortfolio(res.portfolio);
        // Chat may have changed the watchlist / positions — reconcile.
        refreshWatchlist();
        refreshHistory();
      } catch (e) {
        setMessages((m) => [
          ...m,
          {
            role: 'assistant',
            content: e instanceof ApiError ? `Error: ${e.message}` : 'The assistant is unavailable right now.',
          },
        ]);
      } finally {
        setChatLoading(false);
      }
    },
    [refreshWatchlist, refreshHistory],
  );

  const positions = livePortfolio?.positions ?? [];

  return (
    <div className="flex flex-col h-screen">
      <Header portfolio={livePortfolio} status={status} />

      <div className="flex-1 min-h-0 grid grid-cols-[280px_1fr_340px] gap-2 p-2">
        {/* Left: watchlist */}
        <div className="min-h-0">
          <Watchlist
            items={watchlistItems}
            history={history}
            selected={selected}
            onSelect={setSelected}
            onRemove={handleRemove}
            onAdd={handleAdd}
          />
        </div>

        {/* Center: charts + positions + trade */}
        <div className="min-h-0 grid grid-rows-[minmax(0,1.2fr)_minmax(0,1fr)_minmax(0,1fr)_auto] gap-2">
          <div className="min-h-0">
            <MainChart ticker={selected} data={selected ? history[selected] ?? [] : []} />
          </div>
          <div className="min-h-0 grid grid-cols-2 gap-2">
            <PortfolioHeatmap positions={positions} />
            <PnLChart snapshots={snapshots} liveValue={livePortfolio?.total_value ?? null} />
          </div>
          <div className="min-h-0">
            <PositionsTable positions={positions} />
          </div>
          <TradeBar selected={selected} onTrade={handleTrade} />
        </div>

        {/* Right: AI chat */}
        <div className="min-h-0">
          <ChatPanel
            messages={messages}
            loading={chatLoading}
            onSend={handleSend}
            collapsed={chatCollapsed}
            onToggle={() => setChatCollapsed((c) => !c)}
          />
        </div>
      </div>

      {toast && (
        <div
          data-testid="toast"
          className="fixed bottom-4 left-1/2 -translate-x-1/2 bg-terminal-raised border border-terminal-border text-terminal-text text-sm px-4 py-2 rounded-lg shadow-lg z-50"
        >
          {toast}
        </div>
      )}
    </div>
  );
}
