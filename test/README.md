# FinAlly end-to-end tests

Playwright tests that drive the real, containerised app in a browser.

## Run

From the project root (Docker must be running):

```bash
docker compose -f test/docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from playwright
docker compose -f test/docker-compose.test.yml down -v     # clean up
```

The exit code is the test result (0 = all passed). The first run downloads the Playwright image
(about 2 GB) and builds the app; later runs take about a minute.

What the stack does:

| Service | Purpose |
|---|---|
| `webapp` | The **production** image, with `LLM_MOCK=true` (deterministic replies, no API key or network) and an empty in-memory database, so every run starts with $10,000 and the default watchlist |
| `playwright` | Chromium in its own container (browser dependencies stay out of the production image) |

`webapp` is deliberately not called `app`: Chromium force-upgrades `http://app` to HTTPS because
`.app` is an HSTS-preloaded TLD.

## When a test fails

Artifacts for failing tests only are written to `test/test-results/` (full-page screenshot,
Playwright trace, video) and the HTML report to `test/playwright-report/`:

```bash
cd test && npx playwright show-report      # or open a trace: npx playwright show-trace <trace.zip>
```

## Layout

```
test/
  e2e/
    test_full_workflow.spec.ts   the 10-step workflow (serial; each step builds on the last)
    helpers.ts                   parsing, placeTrade(), API cross-checks, flash recorder, severable proxy
    pages/                       page objects: Header, Watchlist, TradeForm, Positions, Chat, Heatmap, PnLChart
  playwright.config.ts
  docker-compose.test.yml
  Dockerfile                     Playwright runner image
```

The workflow steps: fresh start, price streaming and flashes, manual buy, AI question, AI trade,
AI watchlist add, heatmap, sell, P&L chart, SSE reconnect.

## Running against your own instance

The suite needs a **fresh** database (step 1 checks for $10,000 and says so if not). Point it at a
running app with:

```bash
cd test && npm ci && npx playwright install chromium
BASE_URL=http://localhost:8000 npx playwright test
```

Use an instance started with `LLM_MOCK=true`, because steps 4 to 6 rely on the mock replies.

## Notes for maintainers

- The Docker image tag in `Dockerfile` must match the `@playwright/test` version in `package.json`.
- The price-flash classes last only 500ms, so step 2 counts them with a `MutationObserver` instead
  of polling for them.
- Step 10 cuts a real TCP connection through a small in-process proxy. Chromium's
  `context.setOffline()` does **not** close an already-open EventSource, so it can't test reconnects.
- Heatmap colours depend on live P&L, which is usually near zero right after buying. Step 7 asserts
  green/red only when the P&L is clearly positive/negative, and "muted neutral" otherwise.
