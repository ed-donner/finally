# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-02-11)

**Core value:** Live-updating prices, instant fake-money trading, and AI chat that analyzes and trades -- all in a dark terminal aesthetic from one Docker container.
**Current focus:** Phase 10 complete - Packaging & Testing. All plans done.

## Current Position

Phase: 10 of 10 (All phases complete)
Plan: 10-02 complete. Human verification checkpoint pending (Task 3).
Status: E2E testing complete: 14 Playwright tests pass against Docker container with LLM_MOCK=true.
Last activity: 2026-02-11 -- Phase 10 Plan 2 complete (E2E testing)

Progress: [██████████] 100%

## Performance Metrics

**Velocity:**
- Total plans completed: 15
- Average duration: 4min
- Total execution time: 60min

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| Phase 01 P01 | 2min | 2 tasks | 12 files |
| Phase 02 P01 | 3min | 2 tasks | 6 files |
| Phase 02 P02 | 3min | 2 tasks | 7 files |
| Phase 03 P01 | 3min | 2 tasks | 8 files |
| Phase 04 P01 | 4min | 2 tasks | 6 files |
| Phase 05 P01 | 4min | 2 tasks | 9 files |
| Phase 05 P02 | 3min | 2 tasks | 5 files |
| Phase 06 P01 | 3min | 2 tasks | 16 files |
| Phase 07 P01 | 2min | 2 tasks | 7 files |
| Phase 07 P02 | 1min | 1 task | 3 files |
| Phase 08 P01 | 1min | 2 tasks | 5 files |
| Phase 08 P02 | 2min | 2 tasks | 4 files |
| Phase 09 P01 | 1min | 2 tasks | 2 files |
| Phase 10 P01 | 2min | 2 tasks | 8 files |
| Phase 10 P02 | 26min | 2 tasks | 10 files |

**Recent Trend:**
- Last 5 plans: 1min, 2min, 1min, 2min, 26min
- Trend: E2E testing required iterative selector fixes

*Updated after each plan completion*

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- [Roadmap]: 10-phase structure derived from 62 requirements; backend phases 1-5 then frontend 6-9 then packaging 10
- [Roadmap]: Phases 2 and 3 can run in parallel (no dependency between portfolio and watchlist)
- [Phase 01]: aiosqlite with isolation_level=None for explicit transaction control
- [Phase 01]: WAL mode + busy_timeout=5000 for concurrent async access without lock errors
- [Phase 01]: Idempotent seeding: check-before-insert for user, INSERT OR IGNORE for watchlist
- [Phase 02]: SQL-level weighted avg cost via ON CONFLICT DO UPDATE, not Python-side calc
- [Phase 02]: Explicit BEGIN/COMMIT/ROLLBACK for atomic multi-table writes
- [Phase 02]: Service functions take (db, price_cache) as explicit args -- dependency injection pattern
- [Phase 02]: record_snapshot skips positions without current price in cache (graceful startup)
- [Phase 02]: Immediate snapshot after each trade for real-time P&L chart accuracy
- [Phase 02]: Background snapshot loop catches exceptions with logging to stay resilient
- [Phase 03]: Service functions are pure async functions (not classes) taking db connection
- [Phase 03]: Router uses closure-based factory pattern matching create_stream_router
- [Phase 03]: httpx AsyncClient + ASGITransport pattern for testing FastAPI routers
- [Phase 04]: Static mount inside lifespan (after routers) to ensure API routes take priority over SPA catch-all
- [Phase 04]: importlib.reload for test isolation of module-level config (DB_PATH, STATIC_DIR)
- [Phase 05]: extra_body for response_format to bypass LiteLLM OpenRouter capability check
- [Phase 05]: Defensive JSON parsing: model_validate_json with fallback to plain message
- [Phase 05]: Error collection pattern: failed trades/watchlist changes as result entries, not exceptions
- [Phase 05]: No try/except in chat router -- process_chat_message handles all error collection internally
- [Phase 05]: Chat route tests use tuple-yielding fixture (app, db) for DB access alongside HTTP client
- [Phase 06]: Tailwind v4 CSS-first config: @theme in globals.css instead of tailwind.config.js
- [Phase 06]: Zustand selectors pattern: each component selects only needed state slices
- [Phase 06]: Native EventSource for SSE: no custom reconnection logic, rely on browser retry
- [Phase 06]: CSS Grid gap-px with bg-terminal-border for 1px border effect between panels
- [Phase 07]: React key remounting (key=ticker+timestamp) for CSS flash animation trigger
- [Phase 07]: Hand-rolled SVG polyline sparkline rather than charting library for mini-charts
- [Phase 07]: Price history capped at 5000 points per ticker to bound memory usage
- [Phase 07]: lightweight-charts v5 addSeries(LineSeries) API with UTCTimestamp branded type cast
- [Phase 07]: Chart created once in useEffect, data synced separately via setData with full history array
- [Phase 08]: Recharts Treemap with custom content prop (not deprecated Cell) for heatmap
- [Phase 08]: P&L color: linear RGB interpolation red-neutral-green clamped at +/-10%
- [Phase 08]: PnlChart follows exact same lifecycle pattern as ChartPanel (create once, sync data separately)
- [Phase 08]: Intl.NumberFormat for currency formatting consistent with Header.tsx pattern
- [Phase 08]: usePortfolioStore.getState() to check tradeError after async executeTrade completes
- [Phase 08]: Inline composition of PositionsTable + TradeBar in page.tsx grid cell
- [Phase 09]: Optimistic user message: added to list before API call completes
- [Phase 09]: Cross-store refresh only on successful actions (executed trades, applied watchlist changes)
- [Phase 09]: Error messages rendered as assistant bubbles rather than toast/alert
- [Phase 10]: uv cache mount (--mount=type=cache) for faster Docker rebuilds
- [Phase 10]: curl installed in image for docker healthcheck
- [Phase 10]: Single CMD using .venv/bin/uvicorn directly (no activation needed)
- [Phase 10]: Playwright 1.58.2 (latest) with Docker image v1.58.2-noble
- [Phase 10]: Serial test execution (workers: 1) for shared backend state
- [Phase 10]: CSS class selectors (div.flex-col, table.w-full) for precise element scoping
- [Phase 10]: Null guards on Heatmap CustomContent pnl props (Recharts Treemap bug)

### Pending Todos

None yet.

### Blockers/Concerns

None. SSE buffering through Docker networking verified -- health check and frontend load work correctly.

## Session Continuity

Last session: 2026-02-11
Stopped at: Completed 10-02-PLAN.md (E2E testing). Human checkpoint pending.
Resume file: None
