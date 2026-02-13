import { describe, expect, it, vi } from 'vitest';

import { fetchPortfolio, fetchWatchlist, parseSsePayload, removeWatchlistTicker } from '@/src/lib/api';

describe('api helpers', () => {
  it('parses SSE payload into ticker map', () => {
    const payload = parseSsePayload('{"AAPL":{"ticker":"AAPL","price":190,"previous_price":189,"timestamp":1,"change":1,"direction":"up"}}');
    expect(payload.AAPL.price).toBe(190);
    expect(payload.AAPL.direction).toBe('up');
  });

  it('normalizes watchlist payload from mixed shape', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ['aapl', { ticker: 'msft' }],
      }),
    );

    const rows = await fetchWatchlist();
    expect(rows.map((row) => row.ticker)).toEqual(['AAPL', 'MSFT']);
  });

  it('prefers day baseline fields for watchlist percent and group', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          items: [
            {
              ticker: 'aapl',
              sector: 'Tech',
              price: 210,
              previous_price: 209.5,
              day_baseline_price: 200,
              day_change_percent: 5,
              direction: 'up',
            },
          ],
        }),
      }),
    );

    const [row] = await fetchWatchlist();
    expect(row.dayBaselinePrice).toBe(200);
    expect(row.changePercent).toBe(5);
    expect(row.group).toBe('Tech');
  });

  it('computes day percent from previous_close when explicit day percent is missing', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          items: [
            {
              ticker: 'msft',
              price: 315,
              previous_close: 300,
              previous_price: 314,
              direction: 'up',
            },
          ],
        }),
      }),
    );

    const [row] = await fetchWatchlist();
    expect(row.dayBaselinePrice).toBe(300);
    expect(row.changePercent).toBeCloseTo(5);
  });

  it('falls back to default portfolio on fetch error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));

    const portfolio = await fetchPortfolio();
    expect(portfolio.cash_balance).toBe(10000);
    expect(portfolio.positions).toEqual([]);
  });

  it('handles 204 delete responses for watchlist removal', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 204,
        headers: {
          get: () => null,
        },
      }),
    );

    await expect(removeWatchlistTicker('AAPL')).resolves.toBeUndefined();
  });
});
