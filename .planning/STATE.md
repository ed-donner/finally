# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-02-11)

**Core value:** Live-updating prices, instant fake-money trading, and AI chat that analyzes and trades -- all in a dark terminal aesthetic from one Docker container.
**Current focus:** Phase 6 - Frontend Foundation (Plan 1 complete)

## Current Position

Phase: 6 of 10 (Phases 1-6 complete)
Plan: 06-01 complete, Phase 6 done. Ready for Phase 7.
Status: Phase 6 complete (frontend foundation). 171 backend tests passing. Frontend builds to static export.
Last activity: 2026-02-11 -- Phase 6 Plan 1 complete (frontend foundation)

Progress: [██████░░░░] 60%

## Performance Metrics

**Velocity:**
- Total plans completed: 8
- Average duration: 3min
- Total execution time: 25min

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

**Recent Trend:**
- Last 5 plans: 3min, 4min, 4min, 3min, 3min
- Trend: stable

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

### Pending Todos

None yet.

### Blockers/Concerns

- [Research]: SSE buffering through Docker networking needs verification during Phase 10

## Session Continuity

Last session: 2026-02-11
Stopped at: Completed 06-01-PLAN.md (frontend foundation). Phase 6 done. Ready for Phase 7.
Resume file: None
