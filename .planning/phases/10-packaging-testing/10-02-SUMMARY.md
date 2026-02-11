---
phase: 10-packaging-testing
plan: 02
subsystem: testing
tags: [playwright, e2e, docker-compose, chromium, sse, mock-llm]

# Dependency graph
requires:
  - phase: 10-packaging-testing
    provides: Docker image "finally" with multi-stage build
  - phase: 06-frontend-shell
    provides: Next.js static export with all UI components
  - phase: 09-chat-integration
    provides: ChatPanel, mock LLM responses
provides:
  - Playwright E2E test suite (14 tests across 5 spec files)
  - docker-compose.test.yml for CI-friendly test execution
  - Full end-to-end coverage: fresh start, watchlist CRUD, trading, portfolio, AI chat
affects: [deployment, ci-cd]

# Tech tracking
tech-stack:
  added: [playwright, docker-compose-test]
  patterns: [serial-e2e-execution, shared-backend-state, css-class-scoped-selectors]

key-files:
  created:
    - test/package.json
    - test/playwright.config.ts
    - test/tsconfig.json
    - test/tests/fresh-start.spec.ts
    - test/tests/watchlist.spec.ts
    - test/tests/trading.spec.ts
    - test/tests/portfolio.spec.ts
    - test/tests/chat.spec.ts
    - test/docker-compose.test.yml
  modified:
    - frontend/src/components/portfolio/Heatmap.tsx

key-decisions:
  - "Playwright 1.58.2 (latest) with matching Docker image v1.58.2-noble"
  - "Serial execution (workers: 1) to avoid race conditions with shared backend state"
  - "CSS class selectors (div.flex-col, table.w-full) for precise element scoping"
  - "Relative cash assertions (capture-then-compare) instead of absolute $10,000.00"

patterns-established:
  - "Cash display scoping: header div.flex-col with has: Cash text to isolate from Portfolio Value"
  - "Positions table scoping: table.w-full to avoid chart library internal tables"
  - "Chat panel collapse handling: check isVisible then click AI Chat if collapsed"
  - "PriceCell row scoping: .group class selector to target individual watchlist rows"

# Metrics
duration: 26min
completed: 2026-02-11
---

# Phase 10 Plan 02: E2E Testing Summary

**Playwright E2E test suite with 14 tests covering fresh start, watchlist CRUD, trading, portfolio, and mocked AI chat against Docker container with LLM_MOCK=true**

## Performance

- **Duration:** 26 min
- **Started:** 2026-02-11T20:39:14Z
- **Completed:** 2026-02-11T21:05:36Z
- **Tasks:** 2 (Task 3 is human checkpoint, skipped)
- **Files modified:** 10

## Accomplishments
- 14 Playwright E2E tests across 5 spec files, all passing against Docker container
- docker-compose.test.yml orchestrates app + Playwright containers for CI
- Fixed frontend Heatmap crash (undefined pnl in Recharts Treemap CustomContent)
- Test selectors carefully scoped to actual component implementations

## Task Commits

Each task was committed atomically:

1. **Task 1: Playwright project setup and all E2E test specs** - `0ded356` (test)
2. **Heatmap bug fix (Rule 1 deviation)** - `05842ba` (fix)
3. **Task 2: docker-compose.test.yml and refined test selectors** - `7328c2d` (feat)

## Files Created/Modified
- `test/package.json` - Playwright 1.58.2 dependency and test scripts
- `test/tsconfig.json` - TypeScript config for test files
- `test/playwright.config.ts` - Playwright config with serial execution, baseURL from env
- `test/tests/fresh-start.spec.ts` - 4 tests: default tickers, cash balance, SSE connection
- `test/tests/watchlist.spec.ts` - 2 tests: add ticker, remove ticker
- `test/tests/trading.spec.ts` - 3 tests: buy shares, sell shares, sell error
- `test/tests/portfolio.spec.ts` - 2 tests: value updates after trade, positions table columns
- `test/tests/chat.spec.ts` - 3 tests: send/receive, buy action card, watchlist action card
- `test/docker-compose.test.yml` - App service (LLM_MOCK=true, healthcheck) + Playwright test service
- `frontend/src/components/portfolio/Heatmap.tsx` - Null guard on pnl/pnlPercent in Treemap CustomContent

## Decisions Made
- Used Playwright 1.58.2 (latest) instead of plan's 1.50.1 -- plan instructed to check npm for latest
- Serial execution (workers: 1) required because all tests share a single backend database
- Relative cash assertions (capture value, verify it changes) instead of absolute $10,000.00 checks
- Scoped selectors using CSS classes (div.flex-col, table.w-full, .group) to avoid strict mode violations from multiple matching elements

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed Heatmap crash: undefined pnl in Treemap CustomContent**
- **Found during:** Task 2 (test execution verification)
- **Issue:** Recharts Treemap calls CustomContent with props that may not include custom data fields (pnl, pnlPercent) during certain render phases, causing "Cannot read properties of undefined (reading 'toFixed')"
- **Fix:** Added null coalescing (?? 0) for pnl and pnlPercent in CustomContent
- **Files modified:** frontend/src/components/portfolio/Heatmap.tsx
- **Verification:** Debug test confirms no page errors after trade execution
- **Committed in:** 05842ba

**2. [Rule 3 - Blocking] Refined test selectors for strict mode compatibility**
- **Found during:** Task 2 (test execution verification)
- **Issue:** Multiple selectors resolved to multiple elements (two $10,000.00 values, two tables, multiple PYPL texts) causing Playwright strict mode violations
- **Fix:** Scoped selectors using CSS classes and parent containers; used relative assertions
- **Files modified:** All 5 test spec files + playwright.config.ts
- **Verification:** All 14 tests pass with zero strict mode violations
- **Committed in:** 7328c2d (combined with docker-compose.test.yml)

---

**Total deviations:** 2 auto-fixed (1 bug, 1 blocking)
**Impact on plan:** Both fixes essential for tests to pass. Heatmap fix is a genuine production bug. Selector refinements are test-quality improvements matching actual rendered DOM.

## Issues Encountered
- Frontend Heatmap component crashed when positions existed with portfolio data -- root-caused to Recharts Treemap passing undefined custom props during render phases. Fixed with null coalescing guards.
- Tests initially used 5 parallel workers which caused race conditions with shared backend state. Fixed by setting workers: 1 in playwright.config.ts.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Full E2E test suite complete, all tests passing
- Docker container verified working with LLM_MOCK=true
- Task 3 (human verification checkpoint) pending -- user should verify the app visually and review test output

## Self-Check: PASSED

All 10 created/modified files verified present. All 3 task commits (0ded356, 05842ba, 7328c2d) verified in git log.

---
*Phase: 10-packaging-testing*
*Completed: 2026-02-11*
