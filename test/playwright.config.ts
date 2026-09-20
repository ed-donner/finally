import { defineConfig, devices } from "@playwright/test";

/**
 * FinAlly E2E configuration.
 *
 * BASE_URL points at the running app: http://app:8000 inside docker-compose.test.yml, or
 * http://localhost:8000 when you run the suite from your own machine against a running container.
 *
 * The workflow spec is one ordered story that builds on the state left by earlier steps
 * (cash spent, positions bought), so it runs serially in a single worker against a FRESH database.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },

  // Artifacts land in ./test-results (screenshots, traces, videos) and ./playwright-report.
  outputDir: "test-results",
  reporter: [["list"], ["html", { outputFolder: "playwright-report", open: "never" }]],

  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost:8000",
    // A full-page screenshot, a trace and a video are kept for every failing test.
    screenshot: { mode: "only-on-failure", fullPage: true },
    trace: "retain-on-failure",
    video: "retain-on-failure",
  },

  projects: [
    {
      name: "chromium",
      // Wide enough for the desktop layout (chat panel docked beside the dashboard).
      use: { ...devices["Desktop Chrome"], viewport: { width: 1600, height: 1000 } },
    },
  ],
});
