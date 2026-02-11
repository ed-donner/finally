import { test, expect } from '@playwright/test';

test.describe('Watchlist CRUD', () => {
  test('add a new ticker to watchlist', async ({ page }) => {
    await page.goto('/');

    // Wait for initial watchlist to load
    await expect(page.getByText('AAPL', { exact: true }).first()).toBeVisible({ timeout: 15000 });

    // The watchlist add input has placeholder "TICKER"
    // There are two inputs with placeholder TICKER (watchlist and trade bar)
    // The watchlist one is first on the page, paired with a "+" button
    const addInput = page.locator('input[placeholder="TICKER"]').first();
    await addInput.fill('PYPL');
    await addInput.press('Enter');

    // PYPL should appear in the watchlist
    await expect(page.getByText('PYPL', { exact: true }).first()).toBeVisible({ timeout: 10000 });
  });

  test('remove a ticker from watchlist', async ({ page }) => {
    await page.goto('/');

    // Wait for initial load
    await expect(page.getByText('NFLX', { exact: true }).first()).toBeVisible({ timeout: 15000 });

    // Each PriceCell is a div.group containing the ticker span and a remove button
    // Use the .group class to scope to the correct row
    const nflxRow = page.locator('.group', { has: page.getByText('NFLX', { exact: true }) });
    await nflxRow.hover();

    // Click the "x" remove button within this specific row
    await nflxRow.getByRole('button', { name: 'x' }).click({ force: true });

    // NFLX should no longer be visible
    await expect(page.getByText('NFLX', { exact: true })).not.toBeVisible({ timeout: 10000 });
  });
});
