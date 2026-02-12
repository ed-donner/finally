import { expect, test, type Page } from '@playwright/test';

const isRealLlmMode = process.env.REAL_LLM_E2E === 'true';
const hasOpenRouterKey = Boolean(process.env.OPENROUTER_API_KEY?.trim());

const getPanels = (page: Page) => ({
  watchlist: page.getByTestId('panel-watchlist'),
  tradeBar: page.getByTestId('panel-trade-bar'),
  positions: page.getByTestId('panel-positions'),
  chat: page.getByTestId('panel-ai-assistant'),
});

test('health endpoint responds', async ({ request }) => {
  const response = await request.get('/api/health');
  expect(response.ok()).toBeTruthy();

  const payload = await response.json();
  expect(payload.status).toBe('ok');
});

test('terminal loads with key panels and connection state', async ({ page }) => {
  const response = await page.goto('/');
  expect(response?.ok()).toBeTruthy();

  const panels = getPanels(page);

  await expect(page.getByText('FinAlly Terminal')).toBeVisible();
  await expect(panels.watchlist.getByRole('heading', { name: 'Watchlist', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Main Chart', exact: true })).toBeVisible();
  await expect(panels.tradeBar.getByRole('heading', { name: 'Trade Bar', exact: true })).toBeVisible();
  await expect(panels.chat.getByRole('heading', { name: 'AI Assistant', exact: true })).toBeVisible();
  await expect(page.getByTestId('connection-state')).toContainText(/connected|reconnecting/i);
});

test('watchlist add and remove ticker', async ({ page }) => {
  await page.goto('/');

  const panels = getPanels(page);
  const tradeTickerInput = panels.tradeBar.getByTestId('trade-ticker-input');
  const ticker = `QA${Date.now().toString().slice(-4)}`;

  await tradeTickerInput.fill(ticker);
  await tradeTickerInput.press('Enter');

  const row = panels.watchlist.getByTestId(`watchlist-row-${ticker}`);
  await expect(row).toBeVisible();

  await panels.watchlist.getByTestId(`watchlist-remove-${ticker}`).click();
  await expect(panels.watchlist.getByTestId(`watchlist-remove-${ticker}`)).toHaveCount(0);
});

test('buy then sell flow updates positions table', async ({ page }) => {
  await page.goto('/');

  const panels = getPanels(page);
  const tradeTickerInput = panels.tradeBar.getByTestId('trade-ticker-input');
  const tradeQtyInput = panels.tradeBar.getByTestId('trade-quantity-input');

  await tradeTickerInput.fill('ABNB');
  await tradeTickerInput.press('Enter');

  await tradeTickerInput.fill('ABNB');
  await tradeQtyInput.fill('1');
  await panels.tradeBar.getByTestId('trade-buy-button').click();
  await expect(panels.positions.getByTestId('position-row-ABNB')).toBeVisible();

  await tradeTickerInput.fill('ABNB');
  await tradeQtyInput.fill('1');
  await panels.tradeBar.getByTestId('trade-sell-button').click();
  await expect(panels.positions.getByTestId('position-row-ABNB')).toHaveCount(0);
});

test('mock AI chat executes and displays actions inline @mock-llm', async ({ page }) => {
  test.skip(isRealLlmMode, 'Mock chat assertion is disabled in real-LLM mode.');

  await page.goto('/');
  const panels = getPanels(page);

  await panels.chat.getByTestId('chat-input').fill('buy 1 nflx add pypl');
  await panels.chat.getByTestId('chat-send-button').click();

  await expect(panels.chat.getByText('Mock response: I prepared actions based on your request.')).toBeVisible();
  await expect(panels.chat.getByText(/Trades:.*buy.*NFLX/i)).toBeVisible();
  await expect(panels.chat.getByText(/Watchlist:.*add.*PYPL/i)).toBeVisible();
});

test('real AI chat executes watchlist and trade actions via UI and API @real-llm', async ({ page, request }) => {
  test.skip(!isRealLlmMode, 'Run with REAL_LLM_E2E=true to enable real-LLM validation.');
  test.skip(!hasOpenRouterKey, 'Skipping real-LLM E2E: OPENROUTER_API_KEY is not configured.');

  const targetWatchlistTicker = 'PYPL';

  const beforeWatchlist = await request.get('/api/watchlist');
  expect(beforeWatchlist.ok()).toBeTruthy();

  const beforeItems = (await beforeWatchlist.json()).items as Array<{ ticker: string }>;
  if (beforeItems.some((item) => item.ticker === targetWatchlistTicker)) {
    const cleanupResp = await request.delete(`/api/watchlist/${targetWatchlistTicker}`);
    expect([204, 404]).toContain(cleanupResp.status());
  }

  await page.goto('/');
  const panels = getPanels(page);

  const prompt = [
    'Execute these account actions now:',
    '- buy 1 AAPL',
    '- add PYPL to the watchlist',
    'Then briefly confirm what was executed.',
  ].join('\n');

  await panels.chat.getByTestId('chat-input').fill(prompt);
  await panels.chat.getByTestId('chat-send-button').click();

  const assistantCard = panels.chat.locator('article').filter({ hasText: /Trades:|Watchlist:/ }).last();
  await expect(assistantCard).toBeVisible({ timeout: 45_000 });

  const assistantText = (await assistantCard.innerText()).toLowerCase();
  if (
    assistantText.includes('openrouter_api_key')
    || assistantText.includes('llm request failed')
    || assistantText.includes('llm is unavailable')
  ) {
    throw new Error(
      'Real-LLM E2E failed: the backend reported OpenRouter authentication/configuration issues. '
      + 'Verify OPENROUTER_API_KEY and rerun.',
    );
  }

  await expect(assistantCard.getByText(/Trades:.*buy.*AAPL/i)).toBeVisible();
  await expect(assistantCard.getByText(/Watchlist:.*add.*PYPL/i)).toBeVisible();

  await expect(panels.watchlist.getByTestId('watchlist-row-PYPL')).toBeVisible();
  await expect(panels.positions.getByTestId('position-row-AAPL')).toBeVisible();

  const afterWatchlist = await request.get('/api/watchlist');
  expect(afterWatchlist.ok()).toBeTruthy();
  const afterWatchlistPayload = await afterWatchlist.json();
  expect(afterWatchlistPayload.items.some((item: { ticker: string }) => item.ticker === targetWatchlistTicker)).toBeTruthy();

  const afterPortfolio = await request.get('/api/portfolio');
  expect(afterPortfolio.ok()).toBeTruthy();
  const afterPortfolioPayload = await afterPortfolio.json();
  expect(
    afterPortfolioPayload.positions.some((position: { ticker: string; quantity: number }) => (
      position.ticker === 'AAPL' && Number(position.quantity) > 0
    )),
  ).toBeTruthy();
});
