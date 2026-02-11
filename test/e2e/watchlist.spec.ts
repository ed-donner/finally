import { test, expect } from "@playwright/test";

test.describe("Watchlist management", () => {
  test("add a ticker to the watchlist", async ({ page }) => {
    await page.goto("/");

    // Wait for initial watchlist to load
    const watchlistTable = page.locator("table").first();
    await expect(watchlistTable.getByText("AAPL")).toBeVisible({ timeout: 15_000 });

    // Type a new ticker in the "Add ticker" input and submit
    const addInput = page.getByPlaceholder("Add ticker");
    await addInput.fill("PYPL");
    await addInput.press("Enter");

    // Verify PYPL appears in the watchlist
    await expect(page.getByText("PYPL", { exact: true })).toBeVisible({ timeout: 10_000 });
  });

  test("remove a ticker from the watchlist", async ({ page }) => {
    await page.goto("/");

    // Wait for initial watchlist to load
    await expect(page.getByText("NFLX", { exact: true })).toBeVisible({ timeout: 15_000 });

    // Find the NFLX row and click the remove button (x)
    const nflxRow = page.locator("tr", { hasText: "NFLX" });
    const removeButton = nflxRow.locator("button", { hasText: "x" });
    await removeButton.click();

    // Verify NFLX is no longer visible
    await expect(page.getByText("NFLX", { exact: true })).not.toBeVisible({ timeout: 10_000 });
  });
});
