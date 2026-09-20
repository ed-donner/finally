import type { Locator, Page } from "@playwright/test";

export interface HeatCell {
  ticker: string;
  area: number; // rendered rectangle area in px^2 (proportional to portfolio weight)
  pnlPercent: number; // the P&L% the tile was coloured from
  rgb: { r: number; g: number; b: number }; // the tile's fill colour
}

/** Page object for the portfolio treemap (size = weight, colour = P&L). */
export class HeatmapPage {
  readonly container: Locator;
  readonly cells: Locator;

  constructor(private readonly page: Page) {
    this.container = page.getByTestId("heatmap");
    this.cells = page.locator('[data-testid^="heatmap-cell-"]');
  }

  cell(ticker: string): Locator {
    return this.page.getByTestId(`heatmap-cell-${ticker}`);
  }

  async read(ticker: string): Promise<HeatCell> {
    const cell = this.cell(ticker);
    const rect = cell.locator("rect");
    const box = await rect.boundingBox();
    if (!box) throw new Error(`Heatmap cell ${ticker} has no bounding box`);
    const fill = (await rect.getAttribute("fill")) ?? "";
    const m = fill.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
    if (!m) throw new Error(`Unexpected fill "${fill}" on heatmap cell ${ticker}`);
    return {
      ticker,
      area: box.width * box.height,
      pnlPercent: Number(await cell.getAttribute("data-pnl")),
      rgb: { r: Number(m[1]), g: Number(m[2]), b: Number(m[3]) },
    };
  }
}
