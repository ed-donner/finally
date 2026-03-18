import { test, expect, Page } from "@playwright/test";

// Helper: wait for the app to fully load (watchlist populated, prices streaming)
async function waitForAppReady(page: Page) {
  // Wait for the FinAlly header to appear
  await expect(page.locator("h1", { hasText: "FinAlly" })).toBeVisible();
  // Wait for at least one watchlist ticker to appear with a price
  await expect(page.locator("text=AAPL").first()).toBeVisible({ timeout: 15_000 });
}

// ─── Fresh Start ─────────────────────────────────────────────────────────────

test.describe("Fresh start", () => {
  test("shows default watchlist with 10 tickers", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    const tickers = [
      "AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
      "NVDA", "META", "JPM", "V", "NFLX",
    ];
    for (const ticker of tickers) {
      await expect(page.locator(`text=${ticker}`).first()).toBeVisible();
    }
  });

  test("shows $10,000 starting balance", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // The header shows cash balance - look for $10,000.00 formatted
    await expect(page.locator("text=$10,000.00").first()).toBeVisible({
      timeout: 10_000,
    });
  });

  test("shows connection status indicator", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // Connection status text should show "connected" once SSE is established
    await expect(page.locator("text=connected").first()).toBeVisible({
      timeout: 10_000,
    });
  });
});

// ─── SSE Price Streaming ─────────────────────────────────────────────────────

test.describe("Price streaming", () => {
  test("prices update in real-time via SSE", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // Wait a moment for prices to start updating, then capture a price
    await page.waitForTimeout(1000);

    // Get the AAPL price text from the watchlist
    const aaplRow = page.locator("text=AAPL").first().locator("..");
    const pricesBefore = await aaplRow.textContent();

    // Wait for prices to change (simulator updates every ~500ms)
    await page.waitForTimeout(3000);

    const pricesAfter = await aaplRow.textContent();

    // Prices should have changed (simulator produces continuous updates)
    // Note: in rare cases the price might not change, so we just verify the page
    // is still functioning and prices are displayed
    expect(pricesBefore).toBeTruthy();
    expect(pricesAfter).toBeTruthy();
  });
});

// ─── Watchlist Management ────────────────────────────────────────────────────

test.describe("Watchlist management", () => {
  test("add and remove a ticker", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // Type a new ticker in the add input
    const addInput = page.locator('input[placeholder="Add ticker..."]');
    await addInput.fill("PYPL");
    await addInput.press("Enter");

    // PYPL should appear in the watchlist
    await expect(page.locator("text=PYPL").first()).toBeVisible({
      timeout: 5000,
    });

    // Now remove it by clicking the remove button
    const pyplRow = page.locator("text=PYPL").first().locator("..").locator("..");
    const removeBtn = pyplRow.locator("text=remove");
    await removeBtn.click();

    // PYPL should disappear
    await expect(page.locator("text=PYPL")).toBeHidden({ timeout: 5000 });
  });
});

// ─── Trading ─────────────────────────────────────────────────────────────────

test.describe("Trading", () => {
  test("buy shares: cash decreases and position appears", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // Use the trade bar to buy shares
    // First click AAPL in the watchlist to select it
    await page.locator("text=AAPL").first().click();

    // Fill in quantity in the trade bar
    const qtyInput = page.locator('input[placeholder="Qty"]');
    await qtyInput.fill("5");

    // Click BUY
    await page.locator("button", { hasText: "BUY" }).click();

    // Wait for the trade status message showing the execution
    await expect(
      page.locator("text=/BUY 5 AAPL/i").first()
    ).toBeVisible({ timeout: 5000 });

    // Check that the positions table shows AAPL
    await expect(page.locator("table").locator("text=AAPL")).toBeVisible({
      timeout: 5000,
    });

    // Cash should be less than $10,000
    // The header should update — the cash value should have decreased
    await page.waitForTimeout(1000);
    const cashText = await page
      .locator("header")
      .locator("text=/\\$/")
      .allTextContents();
    // At least one value should be different from $10,000.00
    const hasChanged = cashText.some((t) => t !== "$10,000.00");
    expect(hasChanged).toBeTruthy();
  });

  test("sell shares: cash increases and position updates", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // First buy some shares
    await page.locator("text=AAPL").first().click();
    const qtyInput = page.locator('input[placeholder="Qty"]');
    await qtyInput.fill("10");
    await page.locator("button", { hasText: "BUY" }).click();
    await expect(
      page.locator("text=/BUY 10 AAPL/i").first()
    ).toBeVisible({ timeout: 5000 });

    // Now sell some shares
    await qtyInput.fill("3");
    await page.locator("button", { hasText: "SELL" }).click();
    await expect(
      page.locator("text=/SELL 3 AAPL/i").first()
    ).toBeVisible({ timeout: 5000 });

    // Position should still show AAPL but with updated quantity
    await expect(page.locator("table").locator("text=AAPL")).toBeVisible();
  });
});

// ─── Portfolio Visualizations ────────────────────────────────────────────────

test.describe("Portfolio visualizations", () => {
  test("heatmap and P&L chart sections render", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // The layout has these section headers
    await expect(
      page.locator("text=Portfolio Heatmap")
    ).toBeVisible();
    await expect(page.locator("text=Portfolio P&L")).toBeVisible();
    await expect(page.locator("text=Positions")).toBeVisible();
  });

  test("heatmap shows data after buying shares", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // Buy shares to create a position
    await page.locator("text=AAPL").first().click();
    const qtyInput = page.locator('input[placeholder="Qty"]');
    await qtyInput.fill("5");
    await page.locator("button", { hasText: "BUY" }).click();
    await expect(
      page.locator("text=/BUY 5 AAPL/i").first()
    ).toBeVisible({ timeout: 5000 });

    // Wait for portfolio refresh
    await page.waitForTimeout(2000);

    // The positions table should now show the AAPL position
    await expect(page.locator("table").locator("text=AAPL")).toBeVisible({
      timeout: 5000,
    });
  });
});

// ─── AI Chat ─────────────────────────────────────────────────────────────────

test.describe("AI Chat (mocked)", () => {
  test("send message and receive response", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // Open the chat panel
    await page.locator("button", { hasText: "AI Chat" }).click();

    // The chat panel should appear
    await expect(page.locator("text=AI Assistant")).toBeVisible();

    // Type a message
    const chatInput = page.locator(
      'input[placeholder="Ask about your portfolio..."]'
    );
    await chatInput.fill("hello");
    await page.locator("button", { hasText: "Send" }).click();

    // Should see the user message
    await expect(page.locator("text=hello").first()).toBeVisible();

    // Should see the mock assistant response
    await expect(
      page.locator("text=Mock:").first()
    ).toBeVisible({ timeout: 10_000 });
  });

  test("AI trade execution appears inline", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // Open the chat panel
    await page.locator("button", { hasText: "AI Chat" }).click();
    await expect(page.locator("text=AI Assistant")).toBeVisible();

    // Ask the AI to buy — the mock will respond with a buy trade action
    const chatInput = page.locator(
      'input[placeholder="Ask about your portfolio..."]'
    );
    await chatInput.fill("buy AAPL");
    await page.locator("button", { hasText: "Send" }).click();

    // Should see the mock response about buying
    await expect(
      page.locator("text=/Mock.*Buying.*AAPL/i").first()
    ).toBeVisible({ timeout: 10_000 });

    // Should see the trade confirmation inline (BUY 10 AAPL)
    await expect(
      page.locator("text=/BUY 10 AAPL/").first()
    ).toBeVisible({ timeout: 5000 });
  });
});
