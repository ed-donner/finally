import { expect, test } from '@playwright/test';
import { getWatchlistTickers, openApp, resetState } from './helpers';

test.describe('Watchlist management', () => {
  test.beforeEach(async ({ request }) => {
    await resetState(request);
  });

  test('add a ticker via the UI, see it stream, then remove it', async ({ page, request }) => {
    await openApp(page);

    await page.getByTestId('watchlist-add-input').fill('PYPL');
    await page.getByTestId('watchlist-add-button').click();

    const row = page.getByTestId('watchlist-row-PYPL');
    await expect(row).toBeVisible();
    // The new ticker gets a live price from the stream.
    await expect(page.getByTestId('watchlist-price-PYPL')).toHaveText(/\d/, { timeout: 20_000 });
    await expect.poll(() => getWatchlistTickers(request)).toContain('PYPL');

    // Persisted: survives a reload.
    await page.reload();
    await expect(page.getByTestId('watchlist-row-PYPL')).toBeVisible();

    await page.getByTestId('watchlist-row-PYPL').hover();
    await page.getByTestId('watchlist-remove-PYPL').click();
    await expect(page.getByTestId('watchlist-row-PYPL')).toHaveCount(0);
    await expect.poll(() => getWatchlistTickers(request)).not.toContain('PYPL');
  });

  test('lower-case input is normalised to upper-case', async ({ page, request }) => {
    await openApp(page);
    await page.getByTestId('watchlist-add-input').fill('amd');
    await page.getByTestId('watchlist-add-input').press('Enter');
    await expect(page.getByTestId('watchlist-row-AMD')).toBeVisible();
    await expect.poll(() => getWatchlistTickers(request)).toContain('AMD');
  });

  test('remove a default ticker', async ({ page, request }) => {
    await openApp(page);
    await page.getByTestId('watchlist-row-NFLX').hover();
    await page.getByTestId('watchlist-remove-NFLX').click();
    await expect(page.getByTestId('watchlist-row-NFLX')).toHaveCount(0);
    await expect.poll(() => getWatchlistTickers(request)).not.toContain('NFLX');
  });

  test('watchlist changes made elsewhere show up after reload', async ({ page, request }) => {
    const res = await request.post('/api/watchlist', { data: { ticker: 'DIS' } });
    expect(res.status()).toBe(201);
    await openApp(page);
    await expect(page.getByTestId('watchlist-row-DIS')).toBeVisible();
  });
});
