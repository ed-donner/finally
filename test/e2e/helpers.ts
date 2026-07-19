import { expect, type Page } from '@playwright/test';

/** Wait until the app has received at least one live price for `ticker`. */
export async function waitForPrice(page: Page, ticker = 'AAPL'): Promise<void> {
  const cell = page.locator(`[data-testid="watchlist-price"][data-ticker="${ticker}"]`);
  await expect(cell).not.toHaveText('—', { timeout: 15_000 });
}

/** Parse a "$1,234.56" style string into a number. */
export function parseMoney(text: string | null): number {
  if (!text) return NaN;
  return Number.parseFloat(text.replace(/[^0-9.-]/g, ''));
}

export async function cashBalance(page: Page): Promise<number> {
  return parseMoney(await page.getByTestId('cash-balance').textContent());
}
