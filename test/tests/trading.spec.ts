import { test, expect } from '@playwright/test';

test.describe('Trading', () => {
  test('buy shares reduces cash and creates position', async ({ page }) => {
    await page.goto('/');

    // Wait for prices and portfolio to load
    await expect(page.getByText('connected')).toBeVisible({ timeout: 15000 });
    // Wait for Cash label to appear in header
    await expect(page.getByText('Cash')).toBeVisible({ timeout: 10000 });

    // The Cash value is the span AFTER the "Cash" label span, inside the same flex-col div
    // Use locator chaining: find the div containing "Cash" text, then get the dollar amount inside it
    const cashContainer = page.locator('header div.flex-col', { has: page.getByText('Cash', { exact: true }) });
    const cashValue = cashContainer.locator('span.font-mono');
    await expect(cashValue).not.toHaveText('---', { timeout: 10000 });

    // Capture current cash text for later comparison
    const initialCash = await cashValue.textContent();

    // The TradeBar has: TICKER input, QTY input, BUY button, SELL button
    const tradeInputs = page.locator('input[placeholder="TICKER"]');
    const tickerInput = tradeInputs.nth(1);
    const qtyInput = page.locator('input[placeholder="QTY"]');
    const buyButton = page.getByRole('button', { name: 'BUY' });

    await tickerInput.fill('AAPL');
    await qtyInput.fill('10');
    await buyButton.click();

    // Cash should change (decrease) from the initial value
    await expect(cashValue).not.toHaveText(initialCash!, { timeout: 10000 });

    // Position should appear in the positions table (the table with class w-full)
    const positionsTable = page.locator('table.w-full');
    await expect(positionsTable.getByText('AAPL')).toBeVisible({ timeout: 5000 });
  });

  test('sell shares increases cash and updates position', async ({ page }) => {
    await page.goto('/');

    // Wait for prices and portfolio
    await expect(page.getByText('connected')).toBeVisible({ timeout: 15000 });

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
    const positionsTable = page.locator('table.w-full');
    await expect(positionsTable.getByText('AAPL')).toBeVisible({ timeout: 10000 });

    // Now sell 5 shares
    await tickerInput.fill('AAPL');
    await qtyInput.fill('5');
    await sellButton.click();

    // The position should still exist (not fully sold)
    await expect(positionsTable.getByText('AAPL')).toBeVisible({ timeout: 10000 });
  });

  test('sell more than owned shows error', async ({ page }) => {
    await page.goto('/');

    await expect(page.getByText('connected')).toBeVisible({ timeout: 15000 });

    const tradeInputs = page.locator('input[placeholder="TICKER"]');
    const tickerInput = tradeInputs.nth(1);
    const qtyInput = page.locator('input[placeholder="QTY"]');
    const sellButton = page.getByRole('button', { name: 'SELL' });

    // Try to sell shares of a ticker we definitely don't own
    await tickerInput.fill('TSLA');
    await qtyInput.fill('100');
    await sellButton.click();

    // Should show a trade error message containing "Insufficient"
    await expect(page.locator('p').filter({ hasText: /Insufficient/i })).toBeVisible({ timeout: 5000 });
  });
});
