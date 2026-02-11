import { test, expect } from "@playwright/test";

test.describe("Portfolio display", () => {
  test("P&L chart area renders", async ({ page }) => {
    await page.goto("/");

    // The P&L section heading should be visible
    await expect(page.getByRole("heading", { name: "Portfolio P&L" })).toBeVisible({
      timeout: 15_000,
    });
  });

  test("heatmap and positions areas render", async ({ page }) => {
    await page.goto("/");

    // With no additional trades, one of these should be visible
    // Either the heatmap empty state or a heading if positions exist from earlier tests
    const heatmapEmpty = page.getByText("No positions to display");
    const heatmapHeading = page.getByRole("heading", { name: "Portfolio Heatmap" });
    await expect(heatmapEmpty.or(heatmapHeading)).toBeVisible({ timeout: 15_000 });
  });

  test("positions table shows data after a trade", async ({ page }) => {
    // Execute a buy via API to ensure there's at least one position
    const res = await page.request.post("/api/portfolio/trade", {
      data: { ticker: "MSFT", side: "buy", quantity: 3 },
    });
    expect(res.ok()).toBeTruthy();

    await page.goto("/");
    await expect(page.getByText("Live")).toBeVisible({ timeout: 15_000 });

    // Wait for portfolio data to load
    await page.waitForTimeout(3000);

    // The Positions heading should now be visible
    await expect(page.getByRole("heading", { name: "Positions" })).toBeVisible({
      timeout: 10_000,
    });

    // MSFT should appear in the positions table
    const positionsTable = page.locator("table").filter({ hasText: "Qty" });
    await expect(positionsTable.getByText("MSFT")).toBeVisible({ timeout: 10_000 });
  });
});
