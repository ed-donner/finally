import type { Locator, Page } from "@playwright/test";

/** Page object for the manual trade form (market orders only). */
export class TradeFormPage {
  readonly tickerInput: Locator;
  readonly quantityInput: Locator;
  readonly buyButton: Locator;
  readonly sellButton: Locator;

  constructor(page: Page) {
    this.tickerInput = page.getByLabel("Ticker", { exact: true });
    this.quantityInput = page.getByLabel("Quantity", { exact: true });
    // exact: the chat suggestion chips ("Buy 5 AAPL") must not match.
    this.buyButton = page.getByRole("button", { name: "Buy", exact: true });
    this.sellButton = page.getByRole("button", { name: "Sell", exact: true });
  }

  /** Fill the form and press Buy or Sell. Resolves once the click is done, not when the trade settles. */
  async submit(side: "buy" | "sell", ticker: string, quantity: number): Promise<void> {
    await this.tickerInput.fill(ticker);
    await this.quantityInput.fill(String(quantity));
    await (side === "buy" ? this.buyButton : this.sellButton).click();
  }
}
