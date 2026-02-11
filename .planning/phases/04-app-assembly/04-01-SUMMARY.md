---
phase: 04-app-assembly
plan: 01
subsystem: api
tags: [fastapi, lifespan, sse, static-files, spa-fallback, integration-tests]

# Dependency graph
requires:
  - phase: 01-db-layer
    provides: "init_db/close_db for SQLite connection management"
  - phase: 02-portfolio
    provides: "portfolio service, router factory, snapshot task"
  - phase: 03-watchlist
    provides: "watchlist service, router factory, get_watchlist"
provides:
  - "FastAPI app entry point at backend/app/main.py with full lifespan wiring"
  - "SPAStaticFiles for SPA fallback on non-API routes"
  - "Health check endpoint at GET /api/health"
  - "Placeholder index.html until frontend is built"
affects: [05-llm-chat, 06-frontend, 10-docker-packaging]

# Tech tracking
tech-stack:
  added: [asgi-lifespan, httpx]
  patterns: [lifespan-context-manager, spa-static-files, integration-testing-with-lifespan-manager]

key-files:
  created:
    - backend/app/main.py
    - backend/app/static_files.py
    - backend/static/index.html
    - backend/tests/test_app.py
  modified:
    - backend/app/market/stream.py
    - backend/pyproject.toml

key-decisions:
  - "Static mount inside lifespan (after routers) to ensure API routes take priority over SPA catch-all"
  - "importlib.reload for test isolation of module-level config (DB_PATH, STATIC_DIR)"

patterns-established:
  - "Lifespan context manager: single place for all startup/shutdown wiring"
  - "SPAStaticFiles: catch-all mount at / for client-side routing"
  - "Integration testing: LifespanManager + AsyncClient for full-app tests"

# Metrics
duration: 4min
completed: 2026-02-11
---

# Phase 4 Plan 1: App Assembly Summary

**FastAPI app entry point with lifespan wiring of DB, market data, portfolio snapshots, and all routers plus SPA static serving**

## Performance

- **Duration:** 4 min
- **Started:** 2026-02-11T18:14:46Z
- **Completed:** 2026-02-11T18:18:31Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments
- Created `main.py` that wires all backend subsystems into a runnable FastAPI app via lifespan context manager
- SPAStaticFiles enables client-side routing with fallback to index.html for unknown paths
- 6 integration tests verify the full assembled app (health, watchlist, portfolio, trades, static serving, SPA fallback)
- All 145 tests pass (139 existing + 6 new)

## Task Commits

Each task was committed atomically:

1. **Task 1: Create main.py, static_files.py, and placeholder static/index.html** - `df3b0b5` (feat)
2. **Task 2: Integration tests for the assembled app** - `b643719` (test)

## Files Created/Modified
- `backend/app/main.py` - FastAPI app with lifespan wiring all subsystems
- `backend/app/static_files.py` - SPAStaticFiles subclass for SPA fallback
- `backend/static/index.html` - Placeholder page until frontend is built
- `backend/tests/test_app.py` - 6 integration tests for the assembled app
- `backend/app/market/stream.py` - Moved router creation inside factory function
- `backend/pyproject.toml` - Added asgi-lifespan and httpx as dev dependencies

## Decisions Made
- **Static mount inside lifespan:** The SPA catch-all mount at `/` must come after API router registration. Placing it at module level caused it to intercept API routes. Moving it into the lifespan after `include_router` calls ensures correct route priority.
- **importlib.reload for test isolation:** Since `DB_PATH` and `STATIC_DIR` are read at module import time, tests use `monkeypatch.setenv` + `importlib.reload` to get fresh config per test.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Moved static mount from module level into lifespan**
- **Found during:** Task 2 (integration tests)
- **Issue:** Module-level `app.mount("/", SPAStaticFiles(...))` intercepted all routes including `/api/*` because it was registered before API routers (which are added during lifespan). GET `/api/watchlist` returned HTML instead of JSON.
- **Fix:** Moved the static mount into the lifespan, after all `include_router` calls, so API routes are checked first.
- **Files modified:** `backend/app/main.py`
- **Verification:** All 6 integration tests pass, API routes return JSON, static routes return HTML.
- **Committed in:** `b643719` (Task 2 commit)

**2. [Rule 1 - Bug] Moved router creation inside create_stream_router factory**
- **Found during:** Task 2 (integration tests)
- **Issue:** `stream.py` had a module-level `router = APIRouter(...)`. Each call to `create_stream_router()` added a duplicate route handler to the shared router object, causing route accumulation across test runs.
- **Fix:** Moved `router = APIRouter(...)` inside the factory function so each call creates a fresh router.
- **Files modified:** `backend/app/market/stream.py`
- **Verification:** All 145 tests pass with no route duplication.
- **Committed in:** `b643719` (Task 2 commit)

**3. [Rule 3 - Blocking] Added httpx as explicit dev dependency**
- **Found during:** Task 2 (integration tests)
- **Issue:** httpx was a transitive dependency that got removed during `uv sync`. Tests failed with `ModuleNotFoundError: No module named 'httpx'`.
- **Fix:** Added `httpx>=0.28.1` to `[project.optional-dependencies] dev` in pyproject.toml.
- **Files modified:** `backend/pyproject.toml`
- **Verification:** `uv sync --extra dev` installs httpx, all tests run.
- **Committed in:** `b643719` (Task 2 commit)

---

**Total deviations:** 3 auto-fixed (2 bugs, 1 blocking)
**Impact on plan:** All auto-fixes necessary for correctness. No scope creep.

## Issues Encountered
- `uv add --dev` creates a `[dependency-groups]` section instead of adding to `[project.optional-dependencies]` dev. Fixed manually both times (asgi-lifespan and httpx).

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Backend is fully assembled and runnable via `uvicorn app.main:app`
- All API endpoints accessible: health, portfolio, watchlist, stream
- Ready for Phase 5 (LLM chat integration) to add chat routes
- Ready for Phase 6+ (frontend) to replace placeholder index.html

## Self-Check: PASSED

- FOUND: `backend/app/main.py`
- FOUND: `backend/app/static_files.py`
- FOUND: `backend/static/index.html`
- FOUND: `backend/tests/test_app.py`
- FOUND: commit `df3b0b5`
- FOUND: commit `b643719`

---
*Phase: 04-app-assembly*
*Completed: 2026-02-11*
