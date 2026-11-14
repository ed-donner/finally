import { expect, test } from '@playwright/test';
import { apiTrade, getPortfolio, openApp, resetState, waitForPrice } from './helpers';

test.describe('Portfolio visualisation', () => {
  test.beforeEach(async ({ request }) => {
    await resetState(request);
    await waitForPrice(request, 'AAPL');
    await waitForPrice(request, 'MSFT');
  });

  test('heatmap renders one tile per position with P&L colouring', async ({ page, request }) => {
    await apiTrade(request, 'AAPL', 'buy', 5);
    await apiTrade(request, 'MSFT', 'buy', 3);
    await openApp(page);

    const heatmap = page.getByTestId('portfolio-heatmap');
    await expect(heatmap).toBeVisible();
    await expect(heatmap).toContainText('AAPL');
    await expect(heatmap).toContainText('MSFT');

    // Rendered as real graphics (svg rects / divs / canvas) with non-zero size.
    const box = await heatmap.boundingBox();
    expect(box?.width ?? 0).toBeGreaterThan(50);
    expect(box?.height ?? 0).toBeGreaterThan(50);

    // Tiles are coloured from a green/red P&L scale: at least one element inside
    // the heatmap carries a non-transparent fill or background colour.
    const coloured = await heatmap.evaluate((root) => {
      const els = [root, ...Array.from(root.querySelectorAll('*'))] as Element[];
      return els.some((el) => {
        const cs = getComputedStyle(el);
        const fill = el.getAttribute('fill') ?? cs.fill;
        const bg = cs.backgroundColor;
        const isColour = (c: string | null) =>
          !!c && c !== 'none' && c !== 'transparent' && !/rgba\(0, 0, 0, 0\)/.test(c);
        return (el.tagName.toLowerCase() === 'rect' && isColour(fill)) || (el !== root && isColour(bg));
      }) || !!root.querySelector('canvas');
    });
    expect(coloured, 'heatmap tiles should be coloured').toBe(true);
  });

  test('P&L chart has data after a trade', async ({ page, request }) => {
    await apiTrade(request, 'AAPL', 'buy', 2);

    // A snapshot is recorded immediately after each trade.
    const res = await request.get('/api/portfolio/history');
    expect(res.status()).toBe(200);
    const { snapshots } = await res.json();
    expect(snapshots.length).toBeGreaterThan(0);

    await openApp(page);
    const chart = page.getByTestId('pnl-chart');
    await expect(chart).toBeVisible();
    // Chart drew something: a canvas (Lightweight Charts) or an svg path (Recharts).
    await expect(chart.locator('canvas, svg path, svg polyline').first()).toBeAttached();
    const box = await chart.boundingBox();
    expect(box?.height ?? 0).toBeGreaterThan(50);
  });

  test('positions table shows ticker, qty, avg cost, price and P&L', async ({ page, request }) => {
    await apiTrade(request, 'AAPL', 'buy', 7);
    const pos = (await getPortfolio(request)).positions.find((p) => p.ticker === 'AAPL')!;

    await openApp(page);
    const row = page.getByTestId('position-row-AAPL');
    await expect(row).toBeVisible();
    await expect(row).toContainText('AAPL');
    await expect(row).toContainText('7');
    // avg cost appears with 2 dp (allow thousands separator)
    const avg = pos.avg_cost.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    await expect(row).toContainText(new RegExp(avg.replace(/\./g, '\\.').replace(/,/g, ',?')));
    // P&L percent column
    await expect(row).toContainText('%');
  });

  test('header total value reflects cash + positions', async ({ page, request }) => {
    await apiTrade(request, 'AAPL', 'buy', 5);
    await openApp(page);
    const p = await getPortfolio(request);
    // Prices move, so allow a small tolerance.
    await expect
      .poll(async () => {
        const txt = await page.getByTestId('header-total-value').textContent();
        return Math.abs(parseFloat((txt ?? '').replace(/[^0-9.]/g, '')) - p.total_value);
      })
      .toBeLessThan(p.positions_value * 0.05 + 1);
  });
});
