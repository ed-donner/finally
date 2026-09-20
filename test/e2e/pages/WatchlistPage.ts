import type { Locator, Page } from "@playwright/test";
import { parseUsd } from "../helpers";

/** The ten tickers the database is seeded with (PLAN.md section 7). */
export const DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"];

/** Page object for the watchlist grid. */
export class WatchlistPage {
  readonly rows: Locator;

  constructor(private readonly page: Page) {
    this.rows = page.locator('[data-testid^="watch-row-"]');
  }

  row(ticker: string): Locator {
    return this.page.getByTestId(`watch-row-${ticker}`);
  }

  priceCell(ticker: string): Locator {
    return this.page.getByTestId(`watch-price-${ticker}`);
  }

  sparkline(ticker: string): Locator {
    return this.row(ticker).locator("svg path");
  }

  async tickers(): Promise<string[]> {
    const ids = await this.rows.evaluateAll((els) => els.map((e) => e.getAttribute("data-testid") ?? ""));
    return ids.map((id) => id.replace("watch-row-", ""));
  }

  async priceText(ticker: string): Promise<string> {
    return this.priceCell(ticker).innerText();
  }

  async price(ticker: string): Promise<number> {
    return parseUsd(await this.priceText(ticker));
  }

  async select(ticker: string): Promise<void> {
    await this.row(ticker).click();
  }
}
