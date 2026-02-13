import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { useTradingData } from '@/src/hooks/useTradingData';

vi.mock('@/src/lib/api', () => ({
  addWatchlistTicker: vi.fn().mockResolvedValue(undefined),
  removeWatchlistTicker: vi.fn().mockResolvedValue(undefined),
  postTrade: vi.fn().mockResolvedValue(undefined),
  sendChat: vi.fn().mockResolvedValue({ message: 'ok', actions: {} }),
  fetchPortfolio: vi.fn().mockResolvedValue({
    cash_balance: 10000,
    total_value: 10000,
    unrealized_pnl: 0,
    positions: [],
  }),
  fetchPortfolioHistory: vi.fn().mockResolvedValue([]),
  fetchWatchlist: vi.fn().mockResolvedValue([
    {
      ticker: 'AAPL',
      price: 200,
      previousPrice: 199,
      dayBaselinePrice: 195,
      changePercent: 2.5641,
      direction: 'up',
      flash: 'up',
      group: 'Tech',
    },
  ]),
}));

describe('useTradingData', () => {
  it('uses day change fields from stream updates instead of tick-to-tick percent', async () => {
    const { result } = renderHook(() => useTradingData());

    await waitFor(() => {
      expect(result.current.watchlist).toHaveLength(1);
    });

    act(() => {
      result.current.onPriceBatch({
        AAPL: {
          ticker: 'AAPL',
          price: 202,
          previous_price: 201,
          timestamp: 1,
          change: 1,
          direction: 'up',
          day_baseline_price: 206,
          day_change_percent: -1.94,
        },
      });
    });

    expect(result.current.watchlist[0].changePercent).toBe(-1.94);
    expect(result.current.watchlist[0].dayBaselinePrice).toBe(206);
  });

  it('derives day percent from stream day baseline when explicit day change percent is missing', async () => {
    const { result } = renderHook(() => useTradingData());

    await waitFor(() => {
      expect(result.current.watchlist).toHaveLength(1);
    });

    act(() => {
      result.current.onPriceBatch({
        AAPL: {
          ticker: 'AAPL',
          price: 203,
          previous_price: 202.5,
          timestamp: 2,
          change: 0.5,
          direction: 'up',
          day_baseline_price: 200,
        },
      });
    });

    expect(result.current.watchlist[0].changePercent).toBeCloseTo(1.5);
  });
});
