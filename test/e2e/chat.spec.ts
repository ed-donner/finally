import { expect, test, type Page } from '@playwright/test';
import { getPortfolio, getWatchlistTickers, openApp, resetState, waitForPrice } from './helpers';

// Requires the backend to run with LLM_MOCK=true (see TEAM_CONTRACTS §6 mock rules).

async function sendChat(page: Page, text: string) {
  const assistant = page.locator('[data-testid="chat-message"][data-role="assistant"]');
  const before = await assistant.count();
  await page.getByTestId('chat-input').fill(text);
  await page.getByTestId('chat-send-button').click();
  await expect(
    page.locator('[data-testid="chat-message"][data-role="user"]').filter({ hasText: text }).last(),
  ).toBeVisible();
  await expect(assistant).toHaveCount(before + 1, { timeout: 20_000 });
  return assistant.last();
}

test.describe('AI chat (mock LLM)', () => {
  test.beforeEach(async ({ request }) => {
    await resetState(request);
    await waitForPrice(request, 'AAPL');
  });

  test('plain message gets the deterministic mock reply', async ({ page }) => {
    await openApp(page);
    const reply = await sendChat(page, 'How is my portfolio doing?');
    await expect(reply).toContainText("Mock response: I'm FinAlly, your AI trading assistant.");
    await expect(page.getByTestId('chat-loading')).toHaveCount(0);
  });

  test('"buy 1 AAPL" executes a trade shown inline and in positions', async ({ page, request }) => {
    await openApp(page);
    const cashBefore = (await getPortfolio(request)).cash_balance;

    const reply = await sendChat(page, 'buy 1 AAPL');
    await expect(reply).toContainText(/Mock: buying/i);

    const action = page.getByTestId('chat-action').filter({ hasText: 'AAPL' }).last();
    await expect(action).toBeVisible();
    await expect(action).toContainText(/\b(buy|bought)\b/i);

    await expect(page.getByTestId('position-row-AAPL')).toBeVisible();
    const after = await getPortfolio(request);
    expect(after.positions.find((p) => p.ticker === 'AAPL')?.quantity).toBe(1);
    expect(after.cash_balance).toBeLessThan(cashBefore);
  });

  test('chat sell that fails validation reports the failure inline', async ({ page, request }) => {
    await openApp(page);
    await sendChat(page, 'sell 5 MSFT');
    const action = page.getByTestId('chat-action').filter({ hasText: 'MSFT' }).last();
    await expect(action).toBeVisible();
    await expect(action).toContainText(/fail|insufficient|error/i);
    expect((await getPortfolio(request)).positions).toHaveLength(0);
  });

  test('"add PYPL" adds to the watchlist via chat', async ({ page, request }) => {
    await openApp(page);
    await sendChat(page, 'add PYPL');
    await expect(page.getByTestId('chat-action').filter({ hasText: 'PYPL' }).last()).toBeVisible();
    await expect(page.getByTestId('watchlist-row-PYPL')).toBeVisible();
    await expect.poll(() => getWatchlistTickers(request)).toContain('PYPL');
  });

  test('loading indicator shows while waiting for the reply', async ({ page }) => {
    await openApp(page);
    // Delay the chat response so the loading state is observable.
    await page.route('**/api/chat', async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      await new Promise((r) => setTimeout(r, 1500));
      await route.continue();
    });
    await page.getByTestId('chat-input').fill('hello there');
    await page.getByTestId('chat-send-button').click();
    await expect(page.getByTestId('chat-loading')).toBeVisible();
    await expect(page.locator('[data-testid="chat-message"][data-role="assistant"]').last()).toContainText(
      'Mock response',
      { timeout: 20_000 },
    );
    await expect(page.getByTestId('chat-loading')).toHaveCount(0);
  });

  test('conversation is restored after reload', async ({ page }) => {
    await openApp(page);
    const marker = `remember marker ${Date.now()}`;
    await sendChat(page, marker);
    await page.reload();
    await expect(page.getByTestId('chat-message').filter({ hasText: marker })).toBeVisible();
  });
});
