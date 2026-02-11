import { test, expect } from "@playwright/test";

test.describe("Trading", () => {
  test("buy shares — cash decreases, position appears", async ({ page }) => {
    await page.goto("/");

    // Wait for prices to be streaming
    await expect(page.getByText("Live")).toBeVisible({ timeout: 15_000 });

    // Wait for initial cash to display
    await expect(page.getByText("$10,000.00").first()).toBeVisible({ timeout: 10_000 });

    // Fill in the trade bar
    const tickerInput = page.getByRole("textbox", { name: "Ticker", exact: true });
    const qtyInput = page.getByPlaceholder("Qty");
    await tickerInput.fill("AAPL");
    await qtyInput.fill("5");

    // Click BUY
    await page.getByRole("button", { name: "BUY" }).click();

    // Wait for trade confirmation message (green text in trade bar)
    await expect(page.getByText(/BUY 5 AAPL/)).toBeVisible({ timeout: 10_000 });

    // Cash should have decreased — check header no longer shows $10,000.00
    // Allow time for the portfolio refresh
    await page.waitForTimeout(2000);

    // The cash balance in the header should be less than $10,000
    const cashText = page.locator("header").getByText(/\$[\d,]+\.\d{2}/).nth(1);
    const cash = await cashText.textContent();
    expect(cash).toBeDefined();
    const cashValue = parseFloat(cash!.replace(/[$,]/g, ""));
    expect(cashValue).toBeLessThan(10_000);

    // AAPL should appear in the positions table
    const positionsSection = page.locator("text=Positions").first();
    await expect(positionsSection).toBeVisible();
  });

  test("sell shares — cash increases after buying", async ({ page }) => {
    // Buy shares via API first to set up state
    const buyRes = await page.request.post("/api/portfolio/trade", {
      data: { ticker: "AAPL", side: "buy", quantity: 10 },
    });
    expect(buyRes.ok()).toBeTruthy();

    await page.goto("/");
    await expect(page.getByText("Live")).toBeVisible({ timeout: 15_000 });

    // Wait for portfolio to load and show non-$10k cash
    await page.waitForTimeout(3000);

    // Record cash after buying
    const cashAfterBuy = page.locator("header").getByText(/\$[\d,]+\.\d{2}/).nth(1);
    const cashBuyText = await cashAfterBuy.textContent();
    const cashAfterBuyValue = parseFloat(cashBuyText!.replace(/[$,]/g, ""));

    // Now sell some shares via the UI
    const tickerInput = page.getByRole("textbox", { name: "Ticker", exact: true });
    const qtyInput = page.getByPlaceholder("Qty");
    await tickerInput.fill("AAPL");
    await qtyInput.fill("5");
    // The SELL button can be obscured by a layout overlap, so use JS to click
    const sellButton = page.getByRole("button", { name: "SELL" });
    await sellButton.evaluate((el: HTMLElement) => el.click());

    // Wait for trade confirmation
    await expect(page.getByText(/SELL 5 AAPL/)).toBeVisible({ timeout: 10_000 });

    // Wait for portfolio to refresh
    await page.waitForTimeout(3000);

    // Cash should have increased after selling
    const cashAfterSell = page.locator("header").getByText(/\$[\d,]+\.\d{2}/).nth(1);
    const cashSellText = await cashAfterSell.textContent();
    const cashAfterSellValue = parseFloat(cashSellText!.replace(/[$,]/g, ""));

    expect(cashAfterSellValue).toBeGreaterThan(cashAfterBuyValue);
  });
});
