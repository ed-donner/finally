import type { Locator, Page } from "@playwright/test";
import { parseUsd } from "../helpers";

/** Column order of the positions table. */
const COL = { ticker: 0, quantity: 1, avgCost: 2, price: 3, pnl: 4, changePct: 5 } as const;

/** Page object for the positions table. */
export class PositionsPage {
  readonly table: Locator;
  readonly rows: Locator;

  constructor(private readonly page: Page) {
    this.table = page.getByTestId("positions-table");
    this.rows = page.locator('[data-testid^="position-row-"]');
  }

  row(ticker: string): Locator {
    return this.page.getByTestId(`position-row-${ticker}`);
  }

  private cell(ticker: string, col: number): Locator {
    return this.row(ticker).locator("td").nth(col);
  }

  async quantity(ticker: string): Promise<number> {
    return Number((await this.cell(ticker, COL.quantity).innerText()).replace(/,/g, ""));
  }

  async avgCost(ticker: string): Promise<number> {
    return parseUsd(await this.cell(ticker, COL.avgCost).innerText());
  }

  async price(ticker: string): Promise<number> {
    return parseUsd(await this.cell(ticker, COL.price).innerText());
  }

  async pnlPercent(ticker: string): Promise<number> {
    return parseUsd((await this.cell(ticker, COL.changePct).innerText()).replace("%", ""));
  }

  /** Market value = quantity x current price, as displayed. */
  async marketValue(ticker: string): Promise<number> {
    return (await this.quantity(ticker)) * (await this.price(ticker));
  }
}
