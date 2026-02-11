# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-02-11)

**Core value:** Live-updating prices, instant fake-money trading, and AI chat that analyzes and trades -- all in a dark terminal aesthetic from one Docker container.
**Current focus:** Phase 7 complete. Ready for Phase 8 - Portfolio Visualization.

## Current Position

Phase: 7 of 10 (Phases 1-7 complete)
Plan: 07-02 complete. Phase 7 done. Ready for Phase 8.
Status: Watchlist panel and chart panel live. Canvas-based chart with real-time price streaming via lightweight-charts v5.
Last activity: 2026-02-11 -- Phase 7 Plan 2 complete (chart panel)

Progress: [████████░░] 70%

## Performance Metrics

**Velocity:**
- Total plans completed: 10
- Average duration: 3min
- Total execution time: 28min

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

**Recent Trend:**
- Last 5 plans: 4min, 3min, 3min, 2min, 1min
- Trend: improving

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

### Pending Todos

None yet.

### Blockers/Concerns

- [Research]: SSE buffering through Docker networking needs verification during Phase 10

## Session Continuity

Last session: 2026-02-11
Stopped at: Completed 07-02-PLAN.md (chart panel). Phase 7 done. Ready for Phase 8.
Resume file: None
