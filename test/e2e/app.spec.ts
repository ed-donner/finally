import { expect, test } from '@playwright/test';
import { cashBalance, waitForPrice } from './helpers';

test.describe('FinAlly trading workstation', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveTitle(/FinAlly/i);
  });

  test('fresh start: default watchlist, $10k cash, streaming prices', async ({ page }) => {
    // 10 default tickers in the watchlist.
    await expect(page.getByTestId('watchlist-row')).toHaveCount(10);

    // $10,000 starting cash.
    await expect(page.getByTestId('cash-balance')).toHaveText(/\$10,000\.00/);

    // Prices are streaming — AAPL's last price stops being a placeholder.
    await waitForPrice(page, 'AAPL');

    // Connection indicator reports a live stream.
    await expect(page.getByTestId('connection-status')).toHaveAttribute(
      'data-status',
      'connected',
    );
  });

  test('add and remove a watchlist ticker', async ({ page }) => {
    await page.getByTestId('watchlist-add-input').fill('PYPL');
    await page.getByTestId('watchlist-add-button').click();
    await expect(page.locator('[data-testid="watchlist-row"][data-ticker="PYPL"]')).toBeVisible();
    await expect(page.getByTestId('watchlist-row')).toHaveCount(11);

    await page
      .locator('[data-testid="watchlist-remove"][data-ticker="PYPL"]')
      .click({ force: true });
    await expect(
      page.locator('[data-testid="watchlist-row"][data-ticker="PYPL"]'),
    ).toHaveCount(0);
  });

  test('buy shares via the trade bar: cash falls, position appears', async ({ page }) => {
    await waitForPrice(page, 'AAPL');
    const before = await cashBalance(page);

    await page.getByTestId('trade-ticker').fill('AAPL');
    await page.getByTestId('trade-quantity').fill('3');
    await page.getByTestId('trade-buy').click();

    const row = page.locator('[data-testid="positions-row"][data-ticker="AAPL"]');
    await expect(row).toBeVisible();

    await expect
      .poll(async () => await cashBalance(page))
      .toBeLessThan(before);
  });

  test('sell shares: position reduces or disappears', async ({ page }) => {
    // Uses a dedicated ticker (NVDA) so the assertion is independent of any
    // positions other tests may have opened in the shared app instance.
    await waitForPrice(page, 'NVDA');

    await page.getByTestId('trade-ticker').fill('NVDA');
    await page.getByTestId('trade-quantity').fill('2');
    await page.getByTestId('trade-buy').click();
    await expect(
      page.locator('[data-testid="positions-row"][data-ticker="NVDA"]'),
    ).toBeVisible();

    const cashAfterBuy = await cashBalance(page);

    await page.getByTestId('trade-ticker').fill('NVDA');
    await page.getByTestId('trade-quantity').fill('2');
    await page.getByTestId('trade-sell').click();

    // Full sell closes the position; cash rises back.
    await expect(
      page.locator('[data-testid="positions-row"][data-ticker="NVDA"]'),
    ).toHaveCount(0);
    await expect.poll(async () => await cashBalance(page)).toBeGreaterThan(cashAfterBuy);
  });

  test('portfolio visualizations render after a buy', async ({ page }) => {
    await waitForPrice(page, 'MSFT');

    await page.getByTestId('trade-ticker').fill('MSFT');
    await page.getByTestId('trade-quantity').fill('2');
    await page.getByTestId('trade-buy').click();

    // Heatmap gets a cell for the new position.
    await expect(
      page.locator('[data-testid="heatmap-cell"][data-ticker="MSFT"]'),
    ).toBeVisible();

    // Positions table shows the row with a P&L figure.
    await expect(
      page.locator('[data-testid="positions-row"][data-ticker="MSFT"]'),
    ).toBeVisible();
  });

  test('AI chat (mocked) executes a buy inline', async ({ page }) => {
    await waitForPrice(page, 'AAPL');

    await page.getByTestId('chat-input').fill('buy 5 AAPL');
    await page.getByTestId('chat-send').click();

    // Assistant reply appears.
    await expect(page.getByTestId('chat-message').filter({ hasText: /AAPL/i }).last()).toBeVisible();

    // Inline confirmation chip shows the executed action.
    await expect(page.getByTestId('chat-actions').last()).toContainText('AAPL');

    // The position materializes in the table.
    await expect(
      page.locator('[data-testid="positions-row"][data-ticker="AAPL"]'),
    ).toBeVisible();
  });
});
