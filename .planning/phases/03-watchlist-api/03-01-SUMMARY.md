---
phase: 03-watchlist-api
plan: 01
subsystem: api
tags: [fastapi, pydantic, aiosqlite, watchlist, sse, httpx]

# Dependency graph
requires:
  - phase: 01-database-foundation
    provides: "aiosqlite connection with WAL mode, watchlist table schema, seed data"
provides:
  - "Watchlist CRUD service layer (get_watchlist, add_ticker, remove_ticker)"
  - "Watchlist REST API: GET/POST/DELETE /api/watchlist with PriceCache enrichment"
  - "create_watchlist_router factory following closure-based DI pattern"
affects: [04-chat-api, 06-frontend-shell, 09-frontend-chat]

# Tech tracking
tech-stack:
  added: []
  patterns: [closure-based router factory with injected db/cache/source, httpx AsyncClient + ASGITransport for endpoint testing]

key-files:
  created:
    - backend/app/watchlist/__init__.py
    - backend/app/watchlist/models.py
    - backend/app/watchlist/service.py
    - backend/app/watchlist/router.py
    - backend/tests/watchlist/__init__.py
    - backend/tests/watchlist/conftest.py
    - backend/tests/watchlist/test_service.py
    - backend/tests/watchlist/test_router.py
  modified: []

key-decisions:
  - "Service functions are pure async functions taking db connection, not classes"
  - "Router uses closure-based factory pattern matching create_stream_router"
  - "MockMarketDataSource in conftest tracks add/remove calls for assertion"

patterns-established:
  - "httpx AsyncClient + ASGITransport for testing FastAPI routers without a running server"
  - "MockMarketDataSource pattern for testing router-to-market-data integration"

# Metrics
duration: 3min
completed: 2026-02-11
---

# Phase 3 Plan 01: Watchlist API Summary

**Watchlist CRUD API with Pydantic v2 models, async service layer, and closure-based FastAPI router enriching tickers with live PriceCache data**

## Performance

- **Duration:** 3 min
- **Started:** 2026-02-11T17:44:20Z
- **Completed:** 2026-02-11T17:47:13Z
- **Tasks:** 2
- **Files created:** 8

## Accomplishments
- Pydantic v2 request/response models (AddTickerRequest, WatchlistItem, WatchlistResponse)
- Async service layer with case-normalized CRUD: get_watchlist, add_ticker (409 on duplicate), remove_ticker (404 on missing)
- FastAPI router factory with GET (enriched with PriceCache), POST (syncs to MarketDataSource), DELETE (syncs to MarketDataSource)
- 16 tests passing (7 service + 9 router), full 123-test backend suite green, ruff clean

## Task Commits

Each task was committed atomically:

1. **Task 1: Create watchlist models and service layer with tests** - `94f42a4` (feat)
2. **Task 2: Create watchlist router with endpoint tests** - `1e15e71` (feat)

## Files Created/Modified
- `backend/app/watchlist/__init__.py` - Package exports: create_watchlist_router, service functions
- `backend/app/watchlist/models.py` - Pydantic v2 models: AddTickerRequest, WatchlistItem, WatchlistResponse
- `backend/app/watchlist/service.py` - Pure async DB functions: get_watchlist, add_ticker, remove_ticker
- `backend/app/watchlist/router.py` - Factory function create_watchlist_router with GET/POST/DELETE endpoints
- `backend/tests/watchlist/__init__.py` - Test package marker
- `backend/tests/watchlist/conftest.py` - Fixtures: db, MockMarketDataSource, price_cache, httpx client
- `backend/tests/watchlist/test_service.py` - 7 tests for service layer CRUD and edge cases
- `backend/tests/watchlist/test_router.py` - 9 tests for HTTP endpoints including enrichment and market source sync

## Decisions Made
- Service functions are plain async functions (not classes), taking `db: aiosqlite.Connection` as first arg -- matches plan's simplicity requirement
- Router uses closure-based factory pattern identical to `create_stream_router` -- no FastAPI Depends() system
- MockMarketDataSource tracks `added`/`removed` lists for asserting market data source integration without running a real simulator

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Watchlist API is ready for frontend integration (GET returns enriched items with live prices)
- Router factory ready to be mounted on the main FastAPI app in the app assembly phase
- Service functions available for chat API to manage watchlist via LLM commands

## Self-Check: PASSED

- All 8 created files verified present on disk
- Commit 94f42a4 (Task 1) verified in git log
- Commit 1e15e71 (Task 2) verified in git log
- 123/123 backend tests passing (no regressions)
- Ruff lint clean on all watchlist code

---
*Phase: 03-watchlist-api*
*Completed: 2026-02-11*
