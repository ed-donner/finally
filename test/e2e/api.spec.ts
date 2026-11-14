import { expect, test } from '@playwright/test';
import { DEFAULT_TICKERS, apiTrade, getPortfolio, resetState, waitForPrice } from './helpers';

// Contract: planning/TEAM_CONTRACTS.md §5. Errors are {"detail": "..."}.

const expectDetail = async (res: { json: () => Promise<any> }) => {
  const body = await res.json();
  expect(body).toHaveProperty('detail');
};

test.describe('REST API contract', () => {
  test.beforeEach(async ({ request }) => {
    await resetState(request);
  });

  test('GET /api/health', async ({ request }) => {
    const res = await request.get('/api/health');
    expect(res.status()).toBe(200);
    expect(await res.json()).toEqual({ status: 'ok' });
  });

  test('GET /api/portfolio shape', async ({ request }) => {
    const res = await request.get('/api/portfolio');
    expect(res.status()).toBe(200);
    const body = await res.json();
    for (const k of ['cash_balance', 'positions_value', 'total_value', 'unrealized_pnl', 'unrealized_pnl_percent']) {
      expect(typeof body[k], k).toBe('number');
    }
    expect(Array.isArray(body.positions)).toBe(true);
    expect(body.total_value).toBeCloseTo(body.cash_balance + body.positions_value, 1);
  });

  test('POST /api/portfolio/trade buy then sell', async ({ request }) => {
    const price = await waitForPrice(request, 'AAPL');
    const before = await getPortfolio(request);

    const buy = await apiTrade(request, 'AAPL', 'buy', 2);
    expect(buy.trade).toMatchObject({ ticker: 'AAPL', side: 'buy', quantity: 2 });
    for (const k of ['id', 'price', 'executed_at']) expect(buy.trade).toHaveProperty(k);
    expect(buy.trade.price).toBeGreaterThan(price * 0.5);
    const pos = buy.portfolio.positions.find((p: any) => p.ticker === 'AAPL');
    expect(pos).toMatchObject({ ticker: 'AAPL', quantity: 2 });
    for (const k of ['avg_cost', 'current_price', 'market_value', 'unrealized_pnl', 'unrealized_pnl_percent', 'weight']) {
      expect(typeof pos[k], k).toBe('number');
    }
    expect(buy.portfolio.cash_balance).toBeCloseTo(before.cash_balance - 2 * buy.trade.price, 1);

    // fractional shares are supported
    const frac = await apiTrade(request, 'AAPL', 'sell', 0.5);
    expect(frac.trade.quantity).toBe(0.5);

    const sell = await apiTrade(request, 'AAPL', 'sell', 1.5);
    expect(sell.trade.side).toBe('sell');
    expect(sell.portfolio.positions.find((p: any) => p.ticker === 'AAPL')).toBeUndefined();
  });

  test('ticker is case-insensitive on trade', async ({ request }) => {
    await waitForPrice(request, 'MSFT');
    const res = await request.post('/api/portfolio/trade', { data: { ticker: 'msft', side: 'buy', quantity: 1 } });
    expect(res.status()).toBe(200);
    expect((await res.json()).trade.ticker).toBe('MSFT');
  });

  test('trade validation errors return 400', async ({ request }) => {
    await waitForPrice(request, 'AAPL');
    const cases: Array<[string, object]> = [
      ['insufficient shares', { ticker: 'AAPL', side: 'sell', quantity: 1 }],
      ['insufficient cash', { ticker: 'AAPL', side: 'buy', quantity: 1_000_000 }],
      ['zero quantity', { ticker: 'AAPL', side: 'buy', quantity: 0 }],
      ['negative quantity', { ticker: 'AAPL', side: 'buy', quantity: -3 }],
      ['bad side', { ticker: 'AAPL', side: 'short', quantity: 1 }],
    ];
    for (const [name, data] of cases) {
      const res = await request.post('/api/portfolio/trade', { data });
      expect(res.status(), name).toBe(400);
      await expectDetail(res);
    }
    const after = await getPortfolio(request);
    expect(after.positions).toHaveLength(0);
  });

  test('GET /api/portfolio/history returns snapshots oldest first', async ({ request }) => {
    await waitForPrice(request, 'AAPL');
    await apiTrade(request, 'AAPL', 'buy', 1);
    const res = await request.get('/api/portfolio/history');
    expect(res.status()).toBe(200);
    const { snapshots } = await res.json();
    expect(snapshots.length).toBeGreaterThan(0);
    for (const s of snapshots) {
      expect(typeof s.total_value).toBe('number');
      expect(typeof s.recorded_at).toBe('string');
    }
    const times = snapshots.map((s: any) => Date.parse(s.recorded_at));
    expect([...times].sort((a, b) => a - b)).toEqual(times);
  });

  test('GET /api/watchlist returns default tickers with price fields', async ({ request }) => {
    const res = await request.get('/api/watchlist');
    expect(res.status()).toBe(200);
    const { tickers } = await res.json();
    expect(tickers.map((t: any) => t.ticker).sort()).toEqual([...DEFAULT_TICKERS].sort());
    for (const k of ['ticker', 'price', 'previous_price', 'change_percent', 'day_change_percent', 'direction', 'timestamp']) {
      expect(tickers[0], k).toHaveProperty(k);
    }
  });

  test('POST/DELETE /api/watchlist with error codes', async ({ request }) => {
    const add = await request.post('/api/watchlist', { data: { ticker: 'pypl' } });
    expect(add.status()).toBe(201);
    expect((await add.json()).ticker).toBe('PYPL');

    const dup = await request.post('/api/watchlist', { data: { ticker: 'PYPL' } });
    expect(dup.status()).toBe(409);
    await expectDetail(dup);

    for (const bad of ['123', 'TOO-LONG-TICKER', '$$$', '']) {
      const res = await request.post('/api/watchlist', { data: { ticker: bad } });
      expect(res.status(), `invalid ticker "${bad}"`).toBe(400);
    }

    const del = await request.delete('/api/watchlist/PYPL');
    expect(del.status()).toBe(200);
    expect(await del.json()).toEqual({ ticker: 'PYPL', removed: true });

    const missing = await request.delete('/api/watchlist/PYPL');
    expect(missing.status()).toBe(404);
    await expectDetail(missing);
  });

  test('POST /api/chat (mock) and GET /api/chat/history', async ({ request }) => {
    await waitForPrice(request, 'AAPL');
    const plain = await request.post('/api/chat', { data: { message: 'hello' } });
    expect(plain.status()).toBe(200);
    expect(await plain.json()).toEqual({
      message: "Mock response: I'm FinAlly, your AI trading assistant.",
      trades: [],
      watchlist_changes: [],
    });

    const buy = await request.post('/api/chat', { data: { message: 'Please buy 2 AAPL now' } });
    expect(buy.status()).toBe(200);
    const body = await buy.json();
    expect(body.message).toMatch(/^Mock: buying/);
    expect(body.trades).toHaveLength(1);
    expect(body.trades[0]).toMatchObject({ ticker: 'AAPL', side: 'buy', quantity: 2, status: 'executed', error: null });
    expect(typeof body.trades[0].price).toBe('number');
    expect((await getPortfolio(request)).positions.find((p) => p.ticker === 'AAPL')?.quantity).toBe(2);

    const failed = await request.post('/api/chat', { data: { message: 'sell 50 AAPL' } });
    const fb = await failed.json();
    expect(fb.trades[0]).toMatchObject({ ticker: 'AAPL', side: 'sell', status: 'failed' });
    expect(fb.trades[0].error).toBeTruthy();

    const watch = await request.post('/api/chat', { data: { message: 'add PYPL' } });
    const wb = await watch.json();
    expect(wb.watchlist_changes[0]).toMatchObject({ ticker: 'PYPL', action: 'add', status: 'executed' });
    const rm = await request.post('/api/chat', { data: { message: 'remove PYPL' } });
    expect((await rm.json()).watchlist_changes[0]).toMatchObject({ ticker: 'PYPL', action: 'remove', status: 'executed' });

    const empty = await request.post('/api/chat', { data: { message: '' } });
    expect(empty.status()).toBe(400);

    const hist = await request.get('/api/chat/history');
    expect(hist.status()).toBe(200);
    const { messages } = await hist.json();
    expect(messages.length).toBeGreaterThanOrEqual(10);
    const last = messages[messages.length - 1];
    expect(last.role).toBe('assistant');
    for (const k of ['id', 'role', 'content', 'actions', 'created_at']) expect(last).toHaveProperty(k);
    const userMsg = messages.find((m: any) => m.content === 'Please buy 2 AAPL now');
    expect(userMsg?.role).toBe('user');
  });

  test('GET /api/stream/prices emits SSE frames with the full price map', async ({ page }) => {
    await page.goto('/api/health');
    const frame = await page.evaluate(
      () =>
        new Promise<any>((resolve, reject) => {
          const es = new EventSource('/api/stream/prices');
          const timer = setTimeout(() => { es.close(); reject(new Error('no SSE frame in 10s')); }, 10_000);
          es.onmessage = (e) => {
            clearTimeout(timer);
            es.close();
            resolve(JSON.parse(e.data));
          };
        }),
    );
    expect(Object.keys(frame)).toEqual(expect.arrayContaining(DEFAULT_TICKERS));
    const aapl = frame.AAPL;
    for (const k of ['ticker', 'price', 'previous_price', 'timestamp', 'change', 'change_percent', 'direction']) {
      expect(aapl, k).toHaveProperty(k);
    }
    expect(['up', 'down', 'flat']).toContain(aapl.direction);
  });
});
