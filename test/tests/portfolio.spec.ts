import { test, expect } from '@playwright/test';

test.describe('Portfolio Updates', () => {
  test('portfolio value updates after trade', async ({ page }) => {
    await page.goto('/');

    // Wait for Cash value to load (not "---")
    await expect(page.getByText('Cash')).toBeVisible({ timeout: 10000 });
    const cashContainer = page.locator('header div.flex-col', { has: page.getByText('Cash', { exact: true }) });
    const cashValue = cashContainer.locator('span.font-mono');
    await expect(cashValue).not.toHaveText('---', { timeout: 10000 });

    // Capture initial cash
    const initialCash = await cashValue.textContent();

    // Execute a buy trade
    const tradeInputs = page.locator('input[placeholder="TICKER"]');
    const tickerInput = tradeInputs.nth(1);
    const qtyInput = page.locator('input[placeholder="QTY"]');
    const buyButton = page.getByRole('button', { name: 'BUY' });

    await tickerInput.fill('AAPL');
    await qtyInput.fill('5');
    await buyButton.click();

    // Cash should change from initial value
    await expect(cashValue).not.toHaveText(initialCash!, { timeout: 10000 });
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

    // Use the positions table specifically (has class w-full, not the chart's internal table)
    const table = page.locator('table.w-full');
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
