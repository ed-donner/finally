import { test, expect } from '@playwright/test';

test.describe('Trading', () => {
  test('buy shares reduces cash and creates position', async ({ page }) => {
    await page.goto('/');

    // Wait for prices to load so we can trade
    await expect(page.getByText('connected')).toBeVisible({ timeout: 15000 });
    // Wait for portfolio data to load
    await expect(page.getByText('$10,000.00')).toBeVisible({ timeout: 10000 });

    // The TradeBar has: TICKER input, QTY input, BUY button, SELL button
    // TradeBar's ticker input is the second TICKER placeholder (first is watchlist)
    const tradeInputs = page.locator('input[placeholder="TICKER"]');
    const tickerInput = tradeInputs.nth(1);
    const qtyInput = page.locator('input[placeholder="QTY"]');
    const buyButton = page.getByRole('button', { name: 'BUY' });

    await tickerInput.fill('AAPL');
    await qtyInput.fill('10');
    await buyButton.click();

    // Cash should decrease from $10,000.00
    await expect(page.getByText('$10,000.00')).not.toBeVisible({ timeout: 10000 });

    // Position should appear in the positions table
    const positionsArea = page.locator('table');
    await expect(positionsArea.getByText('AAPL')).toBeVisible({ timeout: 5000 });
  });

  test('sell shares increases cash and updates position', async ({ page }) => {
    await page.goto('/');

    // Wait for prices and portfolio
    await expect(page.getByText('connected')).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('$10,000.00')).toBeVisible({ timeout: 10000 });

    const tradeInputs = page.locator('input[placeholder="TICKER"]');
    const tickerInput = tradeInputs.nth(1);
    const qtyInput = page.locator('input[placeholder="QTY"]');
    const buyButton = page.getByRole('button', { name: 'BUY' });
    const sellButton = page.getByRole('button', { name: 'SELL' });

    // First buy some shares
    await tickerInput.fill('AAPL');
    await qtyInput.fill('10');
    await buyButton.click();

    // Wait for position to appear
    const positionsTable = page.locator('table');
    await expect(positionsTable.getByText('AAPL')).toBeVisible({ timeout: 10000 });

    // Now sell 5 shares
    await tickerInput.fill('AAPL');
    await qtyInput.fill('5');
    await sellButton.click();

    // The position should still exist with quantity 5
    const aaplRow = positionsTable.locator('tr', { has: page.getByText('AAPL') });
    await expect(aaplRow.locator('td').nth(1)).toHaveText('5', { timeout: 10000 });
  });

  test('sell more than owned shows error', async ({ page }) => {
    await page.goto('/');

    await expect(page.getByText('connected')).toBeVisible({ timeout: 15000 });

    const tradeInputs = page.locator('input[placeholder="TICKER"]');
    const tickerInput = tradeInputs.nth(1);
    const qtyInput = page.locator('input[placeholder="QTY"]');
    const sellButton = page.getByRole('button', { name: 'SELL' });

    // Try to sell shares we don't own
    await tickerInput.fill('AAPL');
    await qtyInput.fill('100');
    await sellButton.click();

    // Should show a trade error message
    await expect(page.locator('p').filter({ hasText: /insufficient|not enough|no position/i })).toBeVisible({ timeout: 5000 });
  });
});
