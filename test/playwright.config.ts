import { defineConfig, devices } from '@playwright/test';

/**
 * E2E config. Targets a running FinAlly app via BASE_URL.
 *   - docker-compose.test.yml sets BASE_URL=http://app:8000
 *   - locally, default to http://localhost:8000 (or export BASE_URL)
 * Tests assume the app runs with LLM_MOCK=true and the market simulator.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [['list']],
  timeout: 30_000,
  expect: { timeout: 12_000 },
  use: {
    baseURL: process.env.BASE_URL || 'http://localhost:8000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
