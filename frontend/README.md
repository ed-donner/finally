# FinAlly Frontend

Next.js (App Router) + TypeScript + Tailwind, built as a **static export**
(`output: 'export'`). In production FastAPI serves the built `out/` directory;
in dev the Next dev server proxies `/api/*` to the backend.

## Develop

```bash
npm install
# In another terminal, run the backend on :8000 (from ../backend):
#   FINALLY_DB_PATH=../db/finally.db LLM_MOCK=true uv run uvicorn app.main:app --port 8000
npm run dev            # http://localhost:3000, /api/* proxied to :8000
```

`next.config.mjs` rewrites `/api/*` → `BACKEND_ORIGIN` (default `http://localhost:8000`) in dev only.

## Build (static export)

```bash
npm run build          # emits ./out  (served by FastAPI as /app/static in the container)
```

## Test

```bash
npm run test           # Vitest unit tests (components + lib)
```

## Architecture

- `lib/usePriceStream.ts` — subscribes to `GET /api/stream/prices` via `EventSource`.
  **Note:** the stream batches all tickers into one event
  (`data: {"AAPL": {...}, "GOOGL": {...}}`), not one event per ticker.
- `lib/api.ts` — typed same-origin fetch layer; `lib/types.ts` mirrors the backend wire formats.
- `lib/portfolio.ts` — overlays live stream prices onto the REST portfolio for real-time valuation.
- `components/Terminal.tsx` — the orchestrator: owns state, polls REST, wires every panel.

## E2E test IDs

Stable `data-testid` hooks the Playwright suite (`/test`) relies on:

| testid | element |
|--------|---------|
| `connection-status` (+ `data-status`) | header connection dot |
| `cash-balance`, `total-value`, `total-pl-pct` | header figures |
| `watchlist`, `watchlist-row` (`data-ticker`) | watchlist + rows |
| `watchlist-price`, `watchlist-change` (`data-ticker`) | price / change cells |
| `watchlist-add-input`, `watchlist-add-button`, `watchlist-remove` | watchlist controls |
| `main-chart`, `pnl-chart`, `heatmap`, `heatmap-cell` (`data-ticker`) | charts |
| `positions-table`, `positions-row` (`data-ticker`), `position-pl` | positions |
| `trade-bar`, `trade-ticker`, `trade-quantity`, `trade-buy`, `trade-sell` | trade bar |
| `chat-panel`, `chat-input`, `chat-send`, `chat-message` (`data-role`), `chat-actions`, `chat-loading` | AI chat |
| `toast` | transient trade/error notice |
