import { test, expect } from '@playwright/test';

test.describe('Fresh Start', () => {
  test('shows default watchlist tickers', async ({ page }) => {
    await page.goto('/');

    // Default watchlist should show all 10 tickers
    const defaultTickers = ['AAPL', 'GOOGL', 'MSFT', 'AMZN', 'TSLA', 'NVDA', 'META', 'JPM', 'V', 'NFLX'];
    for (const ticker of defaultTickers) {
      await expect(page.getByText(ticker, { exact: true }).first()).toBeVisible({ timeout: 15000 });
    }
  });

  test('shows starting cash balance of $10,000', async ({ page }) => {
    await page.goto('/');

    // Header should show $10,000.00 cash balance
    await expect(page.getByText('$10,000.00')).toBeVisible({ timeout: 10000 });
  });

  test('prices start streaming from SSE', async ({ page }) => {
    await page.goto('/');

    // Wait for the connection status to show "connected"
    await expect(page.getByText('connected')).toBeVisible({ timeout: 15000 });
  });

  test('connection status indicator is visible', async ({ page }) => {
    await page.goto('/');

    // The ConnectionDot component renders the connection status text
    await expect(page.getByText('connected')).toBeVisible({ timeout: 15000 });
  });
});
