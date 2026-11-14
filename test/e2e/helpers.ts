import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';

export const DEFAULT_TICKERS = [
  'AAPL', 'GOOGL', 'MSFT', 'AMZN', 'TSLA', 'NVDA', 'META', 'JPM', 'V', 'NFLX',
];

export const STARTING_CASH = 10_000;

/** Parse "$10,000.00", "10000", "-$1,234.5" etc. into a number. */
export function parseMoney(text: string | null | undefined): number {
  if (!text) return NaN;
  const negative = /-|−|\(/.test(text);
  const cleaned = text.replace(/[^0-9.]/g, '');
  const value = parseFloat(cleaned);
  return negative ? -value : value;
}

export async function readMoney(locator: Locator): Promise<number> {
  return parseMoney(await locator.textContent());
}

export type Position = {
  ticker: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_pnl_percent: number;
  weight: number;
};

export type Portfolio = {
  cash_balance: number;
  positions_value: number;
  total_value: number;
  unrealized_pnl: number;
  unrealized_pnl_percent: number;
  positions: Position[];
};

export async function getPortfolio(request: APIRequestContext): Promise<Portfolio> {
  const res = await request.get('/api/portfolio');
  expect(res.status(), 'GET /api/portfolio').toBe(200);
  return res.json();
}

export async function getWatchlistTickers(request: APIRequestContext): Promise<string[]> {
  const res = await request.get('/api/watchlist');
  expect(res.status(), 'GET /api/watchlist').toBe(200);
  const body = await res.json();
  return body.tickers.map((t: { ticker: string }) => t.ticker);
}

/** Wait until the backend has a live price for `ticker` (it may have just been added). */
export async function waitForPrice(request: APIRequestContext, ticker: string): Promise<number> {
  let price: number | null = null;
  await expect
    .poll(
      async () => {
        const res = await request.get('/api/watchlist');
        const body = await res.json();
        const item = body.tickers.find((t: { ticker: string }) => t.ticker === ticker);
        price = item?.price ?? null;
        return price;
      },
      { message: `price for ${ticker}`, timeout: 15_000 },
    )
    .not.toBeNull();
  return price as unknown as number;
}

export async function apiTrade(
  request: APIRequestContext,
  ticker: string,
  side: 'buy' | 'sell',
  quantity: number,
) {
  const res = await request.post('/api/portfolio/trade', { data: { ticker, side, quantity } });
  expect(res.status(), `trade ${side} ${quantity} ${ticker}: ${await res.text()}`).toBe(200);
  return res.json();
}

/**
 * Bring the shared server back to a known baseline:
 *  - no open positions (sold at market — cash will not be exactly $10k afterwards)
 *  - watchlist == the 10 default tickers
 */
export async function resetState(request: APIRequestContext): Promise<void> {
  const portfolio = await getPortfolio(request);
  for (const p of portfolio.positions) {
    if (p.quantity > 0) await apiTrade(request, p.ticker, 'sell', p.quantity);
  }

  const current = await getWatchlistTickers(request);
  for (const t of current) {
    if (!DEFAULT_TICKERS.includes(t)) {
      const res = await request.delete(`/api/watchlist/${t}`);
      expect([200, 404]).toContain(res.status());
    }
  }
  for (const t of DEFAULT_TICKERS) {
    if (!current.includes(t)) {
      const res = await request.post('/api/watchlist', { data: { ticker: t } });
      expect([201, 409]).toContain(res.status());
    }
  }

  const after = await getPortfolio(request);
  expect(after.positions, 'positions after reset').toHaveLength(0);
}

/** Load the app and wait until the SSE stream is connected. */
export async function openApp(page: Page): Promise<void> {
  await page.goto('/');
  await expect(page.getByTestId('connection-status')).toHaveAttribute('data-status', 'connected', {
    timeout: 20_000,
  });
}

/** Wait for the visible price of `ticker` in the watchlist to change. Returns [before, after]. */
export async function expectPriceToTick(page: Page, ticker: string, timeout = 20_000) {
  const priceCell = page.getByTestId(`watchlist-price-${ticker}`);
  await expect(priceCell).toHaveText(/\d/, { timeout });
  const before = (await priceCell.textContent())?.trim();
  await expect
    .poll(async () => (await priceCell.textContent())?.trim(), {
      message: `${ticker} price should change from ${before}`,
      timeout,
    })
    .not.toBe(before);
  const after = (await priceCell.textContent())?.trim();
  return [before, after];
}

export const watchlistRows = (page: Page) =>
  page.getByTestId('watchlist').locator('[data-testid^="watchlist-row-"]');

export const positionRows = (page: Page) =>
  page.getByTestId('positions-table').locator('[data-testid^="position-row-"]');
