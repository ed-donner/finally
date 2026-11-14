import { defineConfig, devices } from '@playwright/test';

/**
 * FinAlly E2E config.
 *
 * The suite shares one server (and one SQLite DB), so it runs serially with a
 * single worker. Files run in alphabetical order: `00-fresh-start.spec.ts`
 * goes first so it can see the untouched seed state; every other spec resets
 * positions and the watchlist via the API in `beforeEach`.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  // Prices tick every ~500 ms and LLM mock replies are instant, so 60 s per test is generous.
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report', open: 'never' }],
  ],
  outputDir: 'test-results',
  use: {
    baseURL: process.env.BASE_URL ?? 'http://localhost:8000',
    actionTimeout: 10_000,
    navigationTimeout: 20_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    viewport: { width: 1600, height: 1000 },
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1600, height: 1000 } },
    },
  ],
});
