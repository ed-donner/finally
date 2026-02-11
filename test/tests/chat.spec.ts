import { test, expect } from '@playwright/test';

test.describe('AI Chat (Mocked)', () => {
  test('send a message and receive a response', async ({ page }) => {
    await page.goto('/');

    // Wait for app to load
    await expect(page.getByText('connected')).toBeVisible({ timeout: 15000 });

    // The ChatPanel has placeholder "Ask FinAlly..." and a "Send" button
    const chatInput = page.getByPlaceholder('Ask FinAlly...');
    const sendButton = page.getByRole('button', { name: 'Send' });

    // If chat panel is collapsed, open it by clicking the "AI Chat" text
    const chatInputVisible = await chatInput.isVisible();
    if (!chatInputVisible) {
      await page.getByText('AI Chat').click();
      await expect(chatInput).toBeVisible({ timeout: 5000 });
    }

    // Send a generic message (should trigger "default" mock response)
    await chatInput.fill('What is my portfolio worth?');
    await sendButton.click();

    // Mock response: "I can see your portfolio. You have cash available. How can I help?"
    await expect(page.getByText('I can see your portfolio')).toBeVisible({ timeout: 15000 });
  });

  test('chat buy command triggers trade action card', async ({ page }) => {
    await page.goto('/');

    await expect(page.getByText('connected')).toBeVisible({ timeout: 15000 });

    const chatInput = page.getByPlaceholder('Ask FinAlly...');
    const sendButton = page.getByRole('button', { name: 'Send' });

    const chatInputVisible = await chatInput.isVisible();
    if (!chatInputVisible) {
      await page.getByText('AI Chat').click();
      await expect(chatInput).toBeVisible({ timeout: 5000 });
    }

    // Send a "buy" message -- mock.py returns a buy 5 AAPL trade
    await chatInput.fill('Please buy some AAPL');
    await sendButton.click();

    // Mock response: "Done! I've bought 5 shares of AAPL for you."
    await expect(page.getByText("I've bought 5 shares of AAPL")).toBeVisible({ timeout: 15000 });

    // The trade action card renders "BUY 5 AAPL @ $xxx.xx = $xxx.xx"
    // Scope to the chat message area (the scrollable container with space-y-3)
    const chatArea = page.locator('.space-y-3');
    await expect(chatArea.getByText(/BUY\s+5\s+AAPL/)).toBeVisible({ timeout: 5000 });
  });

  test('chat add watchlist command shows watchlist action card', async ({ page }) => {
    await page.goto('/');

    await expect(page.getByText('connected')).toBeVisible({ timeout: 15000 });

    const chatInput = page.getByPlaceholder('Ask FinAlly...');
    const sendButton = page.getByRole('button', { name: 'Send' });

    const chatInputVisible = await chatInput.isVisible();
    if (!chatInputVisible) {
      await page.getByText('AI Chat').click();
      await expect(chatInput).toBeVisible({ timeout: 5000 });
    }

    // Send an "add" message -- mock.py returns watchlist add PYPL
    await chatInput.fill('Add PYPL to my watchlist');
    await sendButton.click();

    // Mock response: "I've added PYPL to your watchlist."
    await expect(page.getByText("added PYPL to your watchlist")).toBeVisible({ timeout: 15000 });

    // The WatchlistCard shows either "+ PYPL added to watchlist" or a status message
    // Verify PYPL appears somewhere in the chat area (user message + response + card)
    await expect(page.getByText('PYPL added to watchlist').or(page.getByText('failed to add PYPL'))).toBeVisible({ timeout: 5000 });
  });
});
