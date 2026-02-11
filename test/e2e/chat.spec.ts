import { test, expect } from "@playwright/test";

test.describe("AI Chat (mocked)", () => {
  test("send a message and receive a response", async ({ page }) => {
    await page.goto("/");

    // Wait for the page to load
    await expect(page.getByText("AI Assistant")).toBeVisible({ timeout: 15_000 });

    // Type a message in the chat input
    const chatInput = page.getByPlaceholder("Ask about your portfolio...");
    await chatInput.fill("hello");
    await page.getByRole("button", { name: "Send" }).click();

    // The user message should appear
    await expect(page.getByText("hello")).toBeVisible({ timeout: 5_000 });

    // Wait for the assistant response — mock returns a greeting with "trading assistant"
    await expect(page.getByText("trading assistant", { exact: false })).toBeVisible({
      timeout: 15_000,
    });
  });

  test("chat shows portfolio analysis when asked", async ({ page }) => {
    await page.goto("/");

    // Wait for page load
    await expect(page.getByText("AI Assistant")).toBeVisible({ timeout: 15_000 });

    // Ask about portfolio
    const chatInput = page.getByPlaceholder("Ask about your portfolio...");
    await chatInput.fill("show my portfolio");
    await page.getByRole("button", { name: "Send" }).click();

    // Mock should respond with portfolio info — look for "portfolio is worth" or "cash"
    // The exact values depend on prior test state, so just check the response pattern
    await expect(
      page.getByText("portfolio is worth", { exact: false })
        .or(page.getByText("in cash", { exact: false }))
    ).toBeVisible({ timeout: 15_000 });
  });
});
