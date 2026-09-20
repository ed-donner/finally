import type { Locator, Page } from "@playwright/test";

/** Page object for the portfolio-value line chart (a Lightweight Charts canvas). */
export class PnLChartPage {
  readonly container: Locator;
  readonly canvas: Locator;
  readonly emptyMessage: Locator;

  constructor(page: Page) {
    this.container = page.getByTestId("pnl-chart");
    this.canvas = this.container.locator("canvas").first();
    // The overlay shown instead of a chart while there is no history to draw.
    this.emptyMessage = page.getByText("No history yet");
  }
}
