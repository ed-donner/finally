---
phase: 06-frontend-foundation
plan: 01
subsystem: ui
tags: [next.js, tailwind-v4, zustand, sse, eventsource, css-grid, static-export]

# Dependency graph
requires:
  - phase: 04-docker-static
    provides: FastAPI static file serving and API routes
  - phase: 02-portfolio
    provides: /api/portfolio REST endpoint
  - phase: 01-foundation
    provides: /api/stream/prices SSE endpoint
provides:
  - Next.js static export app with dark terminal layout shell
  - Zustand price store for SSE price data and connection status
  - Zustand portfolio store for REST portfolio data
  - SSE EventSource hook with auto-reconnect
  - CSS Grid terminal layout with 5 panel regions
  - Header with portfolio value, cash balance, connection indicator
  - Tailwind v4 @theme with terminal/brand color tokens
affects: [07-watchlist-trading, 08-portfolio-viz, 09-chat-ai]

# Tech tracking
tech-stack:
  added: [next.js 16.1.6, react 19.2.3, zustand 5.0.11, tailwind 4, inter-font, jetbrains-mono-font]
  patterns: [zustand-selectors, eventsource-sse, css-grid-terminal-layout, static-export]

key-files:
  created:
    - frontend/src/app/globals.css
    - frontend/src/app/layout.tsx
    - frontend/src/app/page.tsx
    - frontend/src/stores/price-store.ts
    - frontend/src/stores/portfolio-store.ts
    - frontend/src/hooks/use-price-stream.ts
    - frontend/src/lib/api.ts
    - frontend/src/components/layout/Header.tsx
    - frontend/src/components/layout/TerminalGrid.tsx
    - frontend/src/components/ui/ConnectionDot.tsx
    - frontend/src/components/panels/WatchlistPanel.tsx
    - frontend/src/components/panels/ChartPanel.tsx
    - frontend/src/components/panels/PortfolioPanel.tsx
    - frontend/src/components/panels/ChatPanel.tsx
  modified:
    - frontend/next.config.ts
    - .gitignore

key-decisions:
  - "Tailwind v4 CSS-first config: @theme in globals.css instead of tailwind.config.js"
  - "Zustand selectors pattern: each component selects only needed state slices"
  - "Native EventSource for SSE: no custom reconnection logic, rely on browser retry"
  - "CSS Grid gap-px with bg-terminal-border for 1px border effect between panels"

patterns-established:
  - "Zustand store pattern: create<Store>()((set) => ({...})) with typed selectors"
  - "Panel component pattern: 'use client' with h-full w-full p-3 bg-terminal-surface"
  - "Currency formatting: Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD'})"
  - "Grid layout: 12-column grid-rows-2 with col-span assignments on wrapper divs in page.tsx"

# Metrics
duration: 3min
completed: 2026-02-11
---

# Phase 6 Plan 1: Frontend Foundation Summary

**Next.js static-export app with Tailwind v4 dark terminal theme, CSS Grid layout, Zustand stores for prices/portfolio, and native EventSource SSE streaming**

## Performance

- **Duration:** 3 min
- **Started:** 2026-02-11T18:57:12Z
- **Completed:** 2026-02-11T19:00:02Z
- **Tasks:** 2
- **Files modified:** 16

## Accomplishments
- Dark Bloomberg-inspired terminal layout with CSS Grid and 5 panel regions (watchlist, chart, portfolio, positions, chat)
- Zustand price store for SSE data with connection status tracking and portfolio store for REST data
- Native EventSource SSE hook connecting to /api/stream/prices with auto-reconnect
- Header displaying portfolio value, cash balance, and color-coded connection dot (green/yellow/red)
- Static export builds successfully to frontend/out/ for FastAPI serving

## Task Commits

Each task was committed atomically:

1. **Task 1: Scaffold Next.js project with Tailwind v4 dark theme and CSS Grid terminal layout** - `f32a9d1` (feat)
2. **Task 2: Zustand stores, SSE hook, portfolio fetch, and Header with live data** - `d08123f` (feat)

## Files Created/Modified
- `frontend/next.config.ts` - Static export config (output: 'export', images: unoptimized)
- `frontend/src/app/globals.css` - Tailwind v4 @theme with terminal/brand/price colors
- `frontend/src/app/layout.tsx` - Root layout with Inter + JetBrains Mono fonts, dark body
- `frontend/src/app/page.tsx` - Main page wiring SSE stream, portfolio fetch, and terminal grid
- `frontend/src/stores/price-store.ts` - Zustand store for SSE price data and connection status
- `frontend/src/stores/portfolio-store.ts` - Zustand store for portfolio REST data (cash, total value)
- `frontend/src/hooks/use-price-stream.ts` - SSE EventSource hook with auto-reconnect
- `frontend/src/lib/api.ts` - Typed fetch helper for REST API calls
- `frontend/src/components/layout/Header.tsx` - Header with portfolio value, cash, connection dot
- `frontend/src/components/layout/TerminalGrid.tsx` - CSS Grid terminal layout container
- `frontend/src/components/ui/ConnectionDot.tsx` - Connection status indicator (green/yellow/red)
- `frontend/src/components/panels/WatchlistPanel.tsx` - Placeholder watchlist panel
- `frontend/src/components/panels/ChartPanel.tsx` - Placeholder chart panel
- `frontend/src/components/panels/PortfolioPanel.tsx` - Placeholder portfolio panel
- `frontend/src/components/panels/ChatPanel.tsx` - Placeholder chat panel
- `.gitignore` - Added negation for frontend/src/lib/ (was caught by Python lib/ rule)

## Decisions Made
- **Tailwind v4 CSS-first config:** Used @theme in globals.css (not tailwind.config.js) per Tailwind v4 conventions
- **Zustand selector pattern:** Each component selects only the state slices it needs via `usePriceStore((s) => s.field)` to minimize re-renders
- **Native EventSource SSE:** No custom reconnection logic; browser EventSource handles retry automatically per server's retry directive
- **CSS Grid gap-px border effect:** 1px gaps on bg-terminal-border create muted borders between bg-terminal-surface panels

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Fixed .gitignore blocking frontend/src/lib/ directory**
- **Found during:** Task 2 (git add for lib/api.ts)
- **Issue:** Root .gitignore had `lib/` rule (from Python template) catching frontend/src/lib/
- **Fix:** Added `!frontend/src/lib/` negation in .gitignore
- **Files modified:** .gitignore
- **Verification:** git add succeeds, build passes
- **Committed in:** d08123f (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Minor .gitignore fix needed for frontend directory structure. No scope creep.

## Issues Encountered
None beyond the .gitignore fix documented above.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Terminal layout shell ready for real panel implementations (watchlist, chart, portfolio, chat)
- Zustand stores ready to be expanded with positions, trade history, watchlist data
- SSE hook operational -- will stream live prices when backend is running
- All placeholder panels are simple components ready to be replaced with real UI

## Self-Check: PASSED

- All 15 key files verified present on disk
- Commit f32a9d1 (Task 1) verified in git log
- Commit d08123f (Task 2) verified in git log
- `npm run build` produces static export successfully
- `frontend/out/index.html` exists

---
*Phase: 06-frontend-foundation*
*Completed: 2026-02-11*
