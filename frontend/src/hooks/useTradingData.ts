import { useCallback, useEffect, useMemo, useState } from 'react';

import {
  addWatchlistTicker,
  fetchPortfolio,
  fetchPortfolioHistory,
  fetchWatchlist,
  postTrade,
  removeWatchlistTicker,
  sendChat,
} from '@/src/lib/api';
import { toTicker } from '@/src/lib/format';
import {
  ChatMessage,
  ConnectionState,
  Portfolio,
  PortfolioSnapshot,
  PriceUpdate,
  TradeRequest,
  WatchlistItem,
} from '@/src/types/trading';

const maxSparklinePoints = 80;

const appendSeries = (series: number[], next: number): number[] => {
  const merged = [...series, next];
  if (merged.length <= maxSparklinePoints) return merged;
  return merged.slice(merged.length - maxSparklinePoints);
};

export const useTradingData = () => {
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [selectedTicker, setSelectedTicker] = useState<string>('AAPL');
  const [portfolio, setPortfolio] = useState<Portfolio>({
    cash_balance: 10000,
    total_value: 10000,
    unrealized_pnl: 0,
    positions: [],
  });
  const [history, setHistory] = useState<PortfolioSnapshot[]>([]);
  const [tickerHistory, setTickerHistory] = useState<Record<string, number[]>>({});
  const [connectionState, setConnectionState] = useState<ConnectionState>('reconnecting');
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([
    {
      id: 'seed',
      role: 'assistant',
      message: 'FinAlly online. Ask for analysis, trades, or watchlist updates.',
    },
  ]);
  const [isChatLoading, setChatLoading] = useState(false);

  const refreshStaticData = useCallback(async () => {
    const [watchlistRows, portfolioData, historyData] = await Promise.all([
      fetchWatchlist(),
      fetchPortfolio(),
      fetchPortfolioHistory(),
    ]);
    setWatchlist(watchlistRows);
    setPortfolio(portfolioData);
    setHistory(historyData);

    const bootstrapSeries: Record<string, number[]> = {};
    for (const row of watchlistRows) {
      bootstrapSeries[row.ticker] = row.price > 0 ? [row.price] : [];
    }
    setTickerHistory((existing) => ({ ...bootstrapSeries, ...existing }));
    if (!watchlistRows.find((row) => row.ticker === selectedTicker) && watchlistRows[0]) {
      setSelectedTicker(watchlistRows[0].ticker);
    }
  }, [selectedTicker]);

  useEffect(() => {
    void refreshStaticData();
  }, [refreshStaticData]);

  const onPriceBatch = useCallback((batch: Record<string, { price: number; previous_price: number; direction: 'up' | 'down' | 'flat' }>) => {
    setWatchlist((current) =>
      current.map((item) => {
        const incoming = batch[item.ticker];
        if (!incoming) return item;

        const prior = incoming.previous_price || item.price || incoming.price;
        const changePercent = prior === 0 ? 0 : ((incoming.price - prior) / prior) * 100;
        return {
          ...item,
          previousPrice: prior,
          price: incoming.price,
          direction: incoming.direction,
          flash: incoming.direction,
          changePercent,
        };
      }),
    );

    setTickerHistory((current) => {
      const next: Record<string, number[]> = { ...current };
      for (const [ticker, value] of Object.entries(batch)) {
        next[ticker] = appendSeries(next[ticker] ?? [], value.price);
      }
      return next;
    });

    setPortfolio((current) => {
      const updatedPositions = current.positions.map((position) => {
        const incoming = batch[position.ticker];
        if (!incoming) return position;
        const unrealizedPnl = (incoming.price - position.avg_cost) * position.quantity;
        const changePercent = position.avg_cost === 0 ? 0 : ((incoming.price - position.avg_cost) / position.avg_cost) * 100;
        return {
          ...position,
          current_price: incoming.price,
          unrealized_pnl: unrealizedPnl,
          change_percent: changePercent,
        };
      });

      const positionsValue = updatedPositions.reduce((sum, p) => sum + p.current_price * p.quantity, 0);
      const totalValue = current.cash_balance + positionsValue;
      const unrealized = updatedPositions.reduce((sum, p) => sum + p.unrealized_pnl, 0);
      return {
        ...current,
        positions: updatedPositions,
        total_value: totalValue,
        unrealized_pnl: unrealized,
      };
    });
  }, []);

  const selectedSeries = useMemo(() => tickerHistory[selectedTicker] ?? [], [selectedTicker, tickerHistory]);

  const trade = useCallback(
    async (payload: TradeRequest) => {
      await postTrade({
        ticker: toTicker(payload.ticker),
        quantity: payload.quantity,
        side: payload.side,
      });
      await refreshStaticData();
    },
    [refreshStaticData],
  );

  const addTicker = useCallback(
    async (ticker: string) => {
      const normalized = toTicker(ticker);
      if (!normalized) return;
      await addWatchlistTicker(normalized);
      await refreshStaticData();
    },
    [refreshStaticData],
  );

  const removeTicker = useCallback(
    async (ticker: string) => {
      await removeWatchlistTicker(toTicker(ticker));
      await refreshStaticData();
    },
    [refreshStaticData],
  );

  const submitChat = useCallback(
    async (message: string) => {
      const trimmed = message.trim();
      if (!trimmed) return;

      const userMessage: ChatMessage = {
        id: `u-${Date.now()}`,
        role: 'user',
        message: trimmed,
      };
      setChatMessages((current) => [...current, userMessage]);
      setChatLoading(true);

      try {
        const response = await sendChat({ message: trimmed });
        const assistantMessage: ChatMessage = {
          id: `a-${Date.now()}`,
          role: 'assistant',
          message: response.message,
          actions: {
            trades: response.actions?.trades,
            watchlist_changes: response.actions?.watchlist_changes,
          },
        };
        setChatMessages((current) => [...current, assistantMessage]);
      } catch {
        setChatMessages((current) => [
          ...current,
          {
            id: `a-${Date.now()}`,
            role: 'assistant',
            message: 'Unable to reach chat endpoint. Try again shortly.',
          },
        ]);
      } finally {
        setChatLoading(false);
        await refreshStaticData();
      }
    },
    [refreshStaticData],
  );

  const liveWatchlist = useMemo(
    () =>
      watchlist.map((item) => {
        if (item.flash === 'flat') return item;
        return item;
      }),
    [watchlist],
  );

  return {
    watchlist: liveWatchlist,
    selectedTicker,
    setSelectedTicker,
    portfolio,
    history,
    tickerHistory,
    selectedSeries,
    connectionState,
    setConnectionState,
    onPriceBatch,
    trade,
    addTicker,
    removeTicker,
    chatMessages,
    isChatLoading,
    submitChat,
  };
};
