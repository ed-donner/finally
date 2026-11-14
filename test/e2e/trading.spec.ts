import { expect, test, type Page } from '@playwright/test';
import { apiTrade, getPortfolio, openApp, readMoney, resetState, waitForPrice } from './helpers';

async function placeOrder(page: Page, ticker: string, quantity: number, side: 'buy' | 'sell') {
  await page.getByTestId('trade-ticker-input').fill(ticker);
  await page.getByTestId('trade-quantity-input').fill(String(quantity));
  await page.getByTestId(side === 'buy' ? 'trade-buy-button' : 'trade-sell-button').click();
}

test.describe('Trading', () => {
  test.beforeEach(async ({ request }) => {
    await resetState(request);
    await waitForPrice(request, 'AAPL');
  });

  test('buy shares: cash decreases, position row and heatmap tile appear', async ({ page, request }) => {
    await openApp(page);
    const cash = page.getByTestId('header-cash');
    await expect(cash).toHaveText(/\d/);
    const cashBefore = await readMoney(cash);

    await placeOrder(page, 'AAPL', 5, 'buy');

    // Cash goes down by roughly 5 * price.
    await expect.poll(() => readMoney(cash)).toBeLessThan(cashBefore - 1);
    await expect(page.getByTestId('trade-error')).toHaveCount(0);

    const row = page.getByTestId('position-row-AAPL');
    await expect(row).toBeVisible();
    await expect(row).toContainText('AAPL');
    await expect(row).toContainText('5');

    // Heatmap shows the new position.
    await expect(page.getByTestId('portfolio-heatmap')).toContainText('AAPL');

    // Backend agrees.
    const portfolio = await getPortfolio(request);
    const pos = portfolio.positions.find((p) => p.ticker === 'AAPL');
    expect(pos?.quantity).toBe(5);
    expect(portfolio.cash_balance).toBeLessThan(cashBefore);
    await expect.poll(() => readMoney(cash)).toBeCloseTo(portfolio.cash_balance, 0);
  });

  test('sell part of a position: cash increases, quantity updates', async ({ page, request }) => {
    await apiTrade(request, 'AAPL', 'buy', 10);
    await openApp(page);
    const cash = page.getByTestId('header-cash');
    await expect(page.getByTestId('position-row-AAPL')).toContainText('10');
    const cashBefore = await readMoney(cash);

    await placeOrder(page, 'AAPL', 4, 'sell');

    await expect.poll(() => readMoney(cash)).toBeGreaterThan(cashBefore + 1);
    const row = page.getByTestId('position-row-AAPL');
    await expect(row).toBeVisible();
    await expect(row).toContainText('6');
    const pos = (await getPortfolio(request)).positions.find((p) => p.ticker === 'AAPL');
    expect(pos?.quantity).toBe(6);
  });

  test('sell entire position: position row disappears', async ({ page, request }) => {
    await apiTrade(request, 'AAPL', 'buy', 3);
    await openApp(page);
    const cash = page.getByTestId('header-cash');
    await expect(page.getByTestId('position-row-AAPL')).toBeVisible();
    const cashBefore = await readMoney(cash);

    await placeOrder(page, 'AAPL', 3, 'sell');

    await expect.poll(() => readMoney(cash)).toBeGreaterThan(cashBefore + 1);
    await expect(page.getByTestId('position-row-AAPL')).toHaveCount(0);
    expect((await getPortfolio(request)).positions).toHaveLength(0);
  });

  test('selling more than owned shows an error and changes nothing', async ({ page, request }) => {
    await openApp(page);
    const before = await getPortfolio(request);

    await placeOrder(page, 'MSFT', 1, 'sell');

    await expect(page.getByTestId('trade-error')).toBeVisible();
    await expect(page.getByTestId('trade-error')).toContainText(/insufficient|not enough|own/i);
    await expect(page.getByTestId('position-row-MSFT')).toHaveCount(0);
    const after = await getPortfolio(request);
    expect(after.cash_balance).toBe(before.cash_balance);
  });

  test('buying with insufficient cash shows an error', async ({ page, request }) => {
    await openApp(page);
    const before = await getPortfolio(request);

    await placeOrder(page, 'NVDA', 1_000_000, 'buy');

    await expect(page.getByTestId('trade-error')).toBeVisible();
    await expect(page.getByTestId('trade-error')).toContainText(/insufficient|not enough|cash/i);
    const after = await getPortfolio(request);
    expect(after.cash_balance).toBe(before.cash_balance);
    expect(after.positions).toHaveLength(0);
  });
});
