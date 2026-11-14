import { expect, test } from '@playwright/test';
import { expectPriceToTick, openApp } from './helpers';

test.describe('SSE resilience', () => {
  test('status leaves "connected" when the stream drops and recovers afterwards', async ({ page }) => {
    // Note: context.setOffline() does not tear down an already-open EventSource in
    // Chromium, so we simulate the outage at the routing layer instead:
    //   1st request  -> opens, then the server closes it (stream drop)
    //   while down   -> every reconnect attempt fails with a network error
    //   restored     -> requests reach the real backend again
    let networkUp = false;
    let calls = 0;
    await page.route('**/api/stream/prices', async (route) => {
      calls += 1;
      if (networkUp) return route.continue();
      if (calls === 1) {
        return route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'retry: 500\n\n' });
      }
      return route.abort('internetdisconnected');
    });

    await page.goto('/');
    const status = page.getByTestId('connection-status');

    // Dropped and failing to reconnect: yellow/red, never green.
    await expect(status).toHaveAttribute('data-status', /reconnecting|disconnected/, { timeout: 15_000 });
    await expect.poll(() => calls, { timeout: 15_000 }).toBeGreaterThanOrEqual(3);
    await expect(status).not.toHaveAttribute('data-status', 'connected');

    // Network back: the client reconnects on its own and prices stream again.
    networkUp = true;
    await expect(status).toHaveAttribute('data-status', 'connected', { timeout: 30_000 });
    await expectPriceToTick(page, 'AAPL');
  });

  test('server-side stream close is retried automatically', async ({ page }) => {
    // First stream request is answered with an empty, immediately-closed body;
    // EventSource must reconnect on its own and reach "connected".
    let calls = 0;
    await page.route('**/api/stream/prices', async (route) => {
      calls += 1;
      if (calls === 1) {
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'retry: 500\n\n' });
      } else {
        await route.continue();
      }
    });
    await openApp(page);
    expect(calls).toBeGreaterThanOrEqual(2);
    await expectPriceToTick(page, 'AAPL');
  });
});
