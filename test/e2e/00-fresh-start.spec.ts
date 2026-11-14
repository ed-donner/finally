import { expect, test } from '@playwright/test';
import {
  DEFAULT_TICKERS,
  STARTING_CASH,
  expectPriceToTick,
  getPortfolio,
  openApp,
  readMoney,
  watchlistRows,
} from './helpers';

/**
 * Runs first (alphabetical file order, 1 worker) so it sees the seed state of a
 * fresh DB. When pointed at a server that has already been used, the exact
 * $10,000 checks are skipped instead of failing.
 */
test.describe('Fresh start', () => {
  test('shows default watchlist, $10k cash and streaming prices', async ({ page, request }) => {
    const portfolio = await getPortfolio(request);
    const pristine = portfolio.positions.length === 0 && portfolio.cash_balance === STARTING_CASH;

    await openApp(page);

    // Header essentials
    await expect(page.getByTestId('header-cash')).toBeVisible();
    await expect(page.getByTestId('header-total-value')).toBeVisible();
    await expect(page.getByTestId('connection-status')).toHaveAttribute('data-status', 'connected');

    // Default watchlist: 10 tickers
    const watchlist = page.getByTestId('watchlist');
    await expect(watchlist).toBeVisible();
    for (const t of DEFAULT_TICKERS) {
      await expect(page.getByTestId(`watchlist-row-${t}`), `row for ${t}`).toBeVisible();
    }
    if (pristine) {
      await expect(watchlistRows(page)).toHaveCount(DEFAULT_TICKERS.length);
    }

    // Cash + total value
    if (pristine) {
      await expect.poll(() => readMoney(page.getByTestId('header-cash'))).toBe(STARTING_CASH);
      await expect.poll(() => readMoney(page.getByTestId('header-total-value'))).toBe(STARTING_CASH);
    } else {
      test.info().annotations.push({
        type: 'skipped-assertion',
        description: 'Server not pristine — exact $10,000 checks skipped',
      });
      await expect
        .poll(() => readMoney(page.getByTestId('header-cash')))
        .toBeCloseTo(portfolio.cash_balance, 0);
    }

    // Prices are streaming: every default ticker shows a price and AAPL ticks.
    for (const t of DEFAULT_TICKERS) {
      await expect(page.getByTestId(`watchlist-price-${t}`)).toHaveText(/\d/);
    }
    await expectPriceToTick(page, 'AAPL');

    // Main layout panels exist
    for (const id of ['main-chart', 'positions-table', 'portfolio-heatmap', 'pnl-chart', 'chat-panel']) {
      await expect(page.getByTestId(id), id).toBeAttached();
    }
  });

  test('clicking a watchlist ticker selects it in the main chart', async ({ page }) => {
    await openApp(page);
    await page.getByTestId('watchlist-row-MSFT').click();
    await expect(page.getByTestId('selected-ticker')).toContainText('MSFT');
    await expect(page.getByTestId('main-chart')).toBeVisible();

    await page.getByTestId('watchlist-row-TSLA').click();
    await expect(page.getByTestId('selected-ticker')).toContainText('TSLA');
  });
});
