import type { Locator, Page } from "@playwright/test";
import { parseUsd } from "../helpers";

export type StreamStatus = "connected" | "reconnecting" | "disconnected";

/** Page object for the top bar: portfolio value, P&L, cash and the connection dot. */
export class HeaderPage {
  readonly totalValue: Locator;
  readonly pnl: Locator;
  readonly cashBalance: Locator;
  readonly connection: Locator;

  constructor(private readonly page: Page) {
    this.totalValue = page.getByTestId("header-total");
    this.pnl = page.getByTestId("header-pnl");
    this.cashBalance = page.getByTestId("header-cash");
    this.connection = page.getByTestId("connection-status");
  }

  async cash(): Promise<number> {
    return parseUsd(await this.cashBalance.innerText());
  }

  async total(): Promise<number> {
    return parseUsd(await this.totalValue.innerText());
  }

  async status(): Promise<StreamStatus> {
    return (await this.connection.getAttribute("data-status")) as StreamStatus;
  }
}
