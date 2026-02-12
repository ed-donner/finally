import { describe, expect, it, vi } from 'vitest';

import { fetchPortfolio, fetchWatchlist, parseSsePayload } from '@/src/lib/api';

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

  it('falls back to default portfolio on fetch error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));

    const portfolio = await fetchPortfolio();
    expect(portfolio.cash_balance).toBe(10000);
    expect(portfolio.positions).toEqual([]);
  });
});
