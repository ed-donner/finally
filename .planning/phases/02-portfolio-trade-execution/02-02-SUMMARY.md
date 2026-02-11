---
phase: 02-portfolio-trade-execution
plan: 02
subsystem: api
tags: [fastapi, aiosqlite, portfolio, routes, snapshots, httpx, asyncio]

# Dependency graph
requires:
  - phase: 02-portfolio-trade-execution-plan-01
    provides: "execute_trade, get_portfolio, get_portfolio_history service functions; Pydantic models (TradeRequest, TradeResponse, PortfolioResponse, PortfolioHistoryResponse)"
  - phase: 01-database-foundation
    provides: "aiosqlite connection, portfolio_snapshots table schema"
provides:
  - "create_portfolio_router(db, price_cache) -> APIRouter with GET /api/portfolio, POST /api/portfolio/trade, GET /api/portfolio/history"
  - "record_snapshot(db, price_cache) for immediate post-trade snapshots"
  - "start_snapshot_task/stop_snapshot_task for periodic background recording (30s interval)"
  - "httpx AsyncClient + ASGITransport test pattern for portfolio route testing"
affects: [04-app-lifespan, 05-llm-integration, frontend-portfolio, frontend-charts]

# Tech tracking
tech-stack:
  added: [httpx]
  patterns: [closure-based-router-factory, background-asyncio-task, immediate-post-trade-snapshot]

key-files:
  created:
    - backend/app/portfolio/snapshots.py
    - backend/app/routes/__init__.py
    - backend/app/routes/portfolio.py
    - backend/tests/portfolio/test_snapshots.py
    - backend/tests/routes/__init__.py
    - backend/tests/routes/test_portfolio_routes.py
  modified:
    - backend/app/portfolio/__init__.py

key-decisions:
  - "record_snapshot skips positions without current price in cache rather than erroring"
  - "Immediate snapshot after each trade (await record_snapshot) for real-time P&L chart accuracy"
  - "Background snapshot loop uses try/except with logging to prevent one failure from killing the task"
  - "Route-level ValueError catch mapped to HTTP 400; Pydantic validation errors auto-mapped to 422"

patterns-established:
  - "Portfolio route factory: create_portfolio_router(db, price_cache) -> APIRouter, matching watchlist/stream patterns"
  - "Module-level _snapshot_task variable for background task lifecycle management"
  - "httpx AsyncClient + ASGITransport for async route testing (same pattern as watchlist routes)"

# Metrics
duration: 3min
completed: 2026-02-11
---

# Phase 2 Plan 02: Portfolio Routes & Snapshot Task Summary

**FastAPI portfolio endpoints (GET portfolio, POST trade, GET history) with closure-based router factory, plus background snapshot task recording portfolio value every 30s and immediately after trades**

## Performance

- **Duration:** 3 min
- **Started:** 2026-02-11T18:00:00Z
- **Completed:** 2026-02-11T18:03:00Z
- **Tasks:** 2
- **Files modified:** 7

## Accomplishments
- Three portfolio HTTP endpoints via create_portfolio_router factory: GET /api/portfolio (live positions + P&L), POST /api/portfolio/trade (buy/sell with 400/422 validation), GET /api/portfolio/history (snapshots)
- Background snapshot module: record_snapshot computes total_value from cash + positions at current prices; start/stop_snapshot_task for periodic 30s recording
- Immediate post-trade snapshot ensures P&L chart reflects trades in real time
- 16 new tests (5 snapshot + 11 route) all passing; full regression 139 tests green

## Task Commits

Both tasks committed together:

1. **Task 1: Snapshot module and portfolio route factory** - `475db6e` (feat)
2. **Task 2: Snapshot and portfolio route tests** - `475db6e` (feat)

## Files Created/Modified
- `backend/app/portfolio/snapshots.py` - record_snapshot, start_snapshot_task, stop_snapshot_task for background portfolio value recording
- `backend/app/routes/__init__.py` - Routes package init
- `backend/app/routes/portfolio.py` - create_portfolio_router factory with GET portfolio, POST trade, GET history endpoints
- `backend/app/portfolio/__init__.py` - Updated to export snapshot functions
- `backend/tests/portfolio/test_snapshots.py` - 5 tests: cash-only snapshot, positions snapshot, row insertion, task lifecycle, safe stop
- `backend/tests/routes/__init__.py` - Test routes package init
- `backend/tests/routes/test_portfolio_routes.py` - 11 tests: GET portfolio, buy/sell trades, validation errors (400/422), history, post-trade snapshots

## Decisions Made
- record_snapshot skips positions without a current price in cache (graceful degradation during startup)
- Background loop catches all exceptions with logging to prevent one failure from killing the periodic task
- Trade endpoint awaits record_snapshot synchronously (not background task) for immediate P&L accuracy
- Route tests create their own app/client fixtures rather than sharing conftest (different setup from service tests)

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- Sandbox restrictions prevented individual per-task git commits; both tasks committed together in a single commit (475db6e)

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- All Phase 2 portfolio endpoints complete and tested at HTTP level
- Snapshot task ready to be wired into app lifespan (Phase 4)
- Route factory pattern consistent with watchlist (create_watchlist_router) and stream (create_stream_router) -- app assembly in Phase 4 will include all three routers
- Full test suite at 139 tests (db: 14, market: 43, watchlist: 16, portfolio: 25, routes: 11 + 30 other)

## Self-Check: PASSED

- FOUND: backend/app/portfolio/snapshots.py
- FOUND: backend/app/routes/__init__.py
- FOUND: backend/app/routes/portfolio.py
- FOUND: backend/app/portfolio/__init__.py (modified)
- FOUND: backend/tests/portfolio/test_snapshots.py
- FOUND: backend/tests/routes/__init__.py
- FOUND: backend/tests/routes/test_portfolio_routes.py
- FOUND: commit 475db6e
- VERIFIED: 16/16 new tests pass
- VERIFIED: 139/139 full regression tests pass
- VERIFIED: ruff lint clean

---
*Phase: 02-portfolio-trade-execution*
*Completed: 2026-02-11*
