# FinAlly — E2E tests (Playwright)

Browser + API tests for the full app, written against `planning/TEAM_CONTRACTS.md`
(REST §5, LLM mock rules §6, `data-testid`s §7). The backend **must** run with
`LLM_MOCK=true`.

| Spec | Covers |
|---|---|
| `e2e/00-fresh-start.spec.ts` | 10 default tickers, $10,000 cash, prices streaming, ticker selection (runs first) |
| `e2e/watchlist.spec.ts` | add / remove tickers via UI, normalisation, persistence |
| `e2e/trading.spec.ts` | buy, partial sell, full sell, insufficient cash / shares errors |
| `e2e/portfolio-viz.spec.ts` | heatmap tiles + colour, P&L chart data, positions table, header total |
| `e2e/chat.spec.ts` | mock chat reply, `buy 1 AAPL` inline action + position, failed trade, watchlist via chat, loading state, history restore |
| `e2e/sse-resilience.spec.ts` | offline → status not connected → back online → `connected` + prices resume; auto-retry |
| `e2e/api.spec.ts` | every REST endpoint, response shapes, 400/404/409 error codes, SSE frame shape |

Tests share one server, so they run serially (1 worker). Each spec resets state in
`beforeEach` (sells all positions, restores the default watchlist via the API).
Cash is not reset, so only the fresh-start spec asserts the exact $10,000 — and it
skips that assertion if the server is not pristine.

## Run with Docker (recommended — fresh DB every run)

From the repo root:

```bash
docker compose -f test/docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from playwright
docker compose -f test/docker-compose.test.yml down -v
```

The `app` service is built from the root `Dockerfile` with `LLM_MOCK=true` and a tmpfs
DB; `playwright` waits for `/api/health`, then runs the suite against `http://finally:8000` (not `app`: Chromium HSTS-preloads the `.app` TLD, which forces HTTPS).
The HTML report lands in `test/playwright-report/` (`cd test && npx playwright show-report`).

## Run against a locally running server

```bash
# 1. build the frontend export
cd frontend && npm ci && npm run build && cd ..

# 2. start the backend with the export, a throwaway DB and the mock LLM
cd backend
FINALLY_STATIC_DIR=../frontend/out FINALLY_DB_PATH=$TMPDIR/e2e.db LLM_MOCK=true \
  uv run uvicorn app.main:app --port 8000
# (delete $TMPDIR/e2e.db first for a fresh start)

# 3. in another terminal
cd test
npm install
npx playwright install chromium
npx playwright test                 # all
npx playwright test e2e/api.spec.ts # one file
BASE_URL=http://localhost:9000 npx playwright test   # different server
```

Keep the `@playwright/test` version in `package.json` and the
`mcr.microsoft.com/playwright:v<version>-noble` tag in `docker-compose.test.yml` in sync.
