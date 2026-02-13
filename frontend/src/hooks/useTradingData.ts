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

const resolveIncomingDayBaseline = (incoming: PriceUpdate, existingBaseline: number, fallbackPrevious: number): number => {
  const baseline = incoming.day_baseline_price ?? incoming.previous_close ?? existingBaseline;
  if (baseline && baseline > 0) return baseline;
  if (fallbackPrevious > 0) return fallbackPrevious;
  return incoming.price;
};

const resolveIncomingDayChangePercent = (incoming: PriceUpdate, dayBaselinePrice: number, existingPercent: number): number => {
  if (typeof incoming.day_change_percent === 'number' && Number.isFinite(incoming.day_change_percent)) {
    return incoming.day_change_percent;
  }
  if (dayBaselinePrice > 0) {
    return ((incoming.price - dayBaselinePrice) / dayBaselinePrice) * 100;
  }
  return existingPercent;
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
      const bootstrapPoint = row.price || row.dayBaselinePrice || row.previousPrice || 0;
      bootstrapSeries[row.ticker] = bootstrapPoint > 0 ? [bootstrapPoint] : [];
    }
    setTickerHistory((existing) => {
      const merged: Record<string, number[]> = { ...existing };
      for (const [ticker, bootstrap] of Object.entries(bootstrapSeries)) {
        const current = existing[ticker] ?? [];
        merged[ticker] = current.length > 0 ? current : bootstrap;
      }
      return merged;
    });
    if (!watchlistRows.find((row) => row.ticker === selectedTicker) && watchlistRows[0]) {
      setSelectedTicker(watchlistRows[0].ticker);
    }
  }, [selectedTicker]);

  useEffect(() => {
    void refreshStaticData();
  }, [refreshStaticData]);

  const onPriceBatch = useCallback((batch: Record<string, PriceUpdate>) => {
    setWatchlist((current) =>
      current.map((item) => {
        const incoming = batch[item.ticker];
        if (!incoming) return item;

        const prior = incoming.previous_price || item.price || incoming.price;
        const dayBaselinePrice = resolveIncomingDayBaseline(incoming, item.dayBaselinePrice, prior);
        const changePercent = resolveIncomingDayChangePercent(incoming, dayBaselinePrice, item.changePercent);
        return {
          ...item,
          previousPrice: prior,
          price: incoming.price,
          dayBaselinePrice,
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

  const selectedSeries = useMemo(() => {
    const currentSeries = tickerHistory[selectedTicker] ?? [];
    if (currentSeries.length > 0) return currentSeries;

    const selectedWatchlistRow = watchlist.find((item) => item.ticker === selectedTicker);
    if (!selectedWatchlistRow) return currentSeries;

    const fallbackPrice =
      selectedWatchlistRow.price
      || selectedWatchlistRow.dayBaselinePrice
      || selectedWatchlistRow.previousPrice;

    return fallbackPrice > 0 ? [fallbackPrice] : currentSeries;
  }, [selectedTicker, tickerHistory, watchlist]);

  useEffect(() => {
    const selectedWatchlistRow = watchlist.find((item) => item.ticker === selectedTicker);
    if (!selectedWatchlistRow) return;

    setTickerHistory((current) => {
      const existingSeries = current[selectedTicker] ?? [];
      if (existingSeries.length > 0) return current;

      const fallbackPoint =
        selectedWatchlistRow.price
        || selectedWatchlistRow.dayBaselinePrice
        || selectedWatchlistRow.previousPrice
        || 0;

      if (fallbackPoint <= 0) return current;
      return { ...current, [selectedTicker]: [fallbackPoint] };
    });
  }, [selectedTicker, watchlist]);

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

  return {
    watchlist,
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
