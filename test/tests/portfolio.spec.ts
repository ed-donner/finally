import { test, expect } from '@playwright/test';

test.describe('Portfolio Updates', () => {
  test('portfolio value updates after trade', async ({ page }) => {
    await page.goto('/');

    // Wait for initial state
    await expect(page.getByText('$10,000.00')).toBeVisible({ timeout: 10000 });

    // Execute a buy trade
    const tradeInputs = page.locator('input[placeholder="TICKER"]');
    const tickerInput = tradeInputs.nth(1);
    const qtyInput = page.locator('input[placeholder="QTY"]');
    const buyButton = page.getByRole('button', { name: 'BUY' });

    await tickerInput.fill('AAPL');
    await qtyInput.fill('5');
    await buyButton.click();

    // Cash should decrease
    await expect(page.getByText('$10,000.00')).not.toBeVisible({ timeout: 10000 });
  });

  test('positions table shows correct columns', async ({ page }) => {
    await page.goto('/');

    // Wait for load
    await expect(page.getByText('connected')).toBeVisible({ timeout: 15000 });

    // Buy shares to create a position
    const tradeInputs = page.locator('input[placeholder="TICKER"]');
    const tickerInput = tradeInputs.nth(1);
    const qtyInput = page.locator('input[placeholder="QTY"]');
    const buyButton = page.getByRole('button', { name: 'BUY' });

    await tickerInput.fill('MSFT');
    await qtyInput.fill('3');
    await buyButton.click();

    // Positions table should have column headers
    const table = page.locator('table');
    await expect(table).toBeVisible({ timeout: 10000 });
    await expect(table.getByText('Ticker')).toBeVisible();
    await expect(table.getByText('Qty')).toBeVisible();
    await expect(table.getByText('Avg Cost')).toBeVisible();
    await expect(table.getByText('Price')).toBeVisible();
    await expect(table.getByText('P&L')).toBeVisible();

    // MSFT should appear in the table
    await expect(table.getByText('MSFT')).toBeVisible();
  });
});
