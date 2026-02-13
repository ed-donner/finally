# FinAlly Watchlist + UI Layout Overhaul (2026-02-13)

## Scope

This document describes the full, implemented UI/data update delivered for the watchlist redesign and subsequent layout refinements.

Primary objectives completed:

- Watchlist redesigned into a desktop **5-column x 10-row** sector grid.
- New default DB watchlist expanded to **50 tickers** (5 sectors x 10).
- Watchlist coloring switched to **same-business-day baseline change**.
- Massive ticker availability verified for all default symbols.
- Middle/right layout reworked to fit viewport height and avoid clipping.
- Right-column overflow issues resolved with a final 3-column top-level grid.
- Main chart population hardened so selecting watchlist tickers always renders data.
- Tests updated and validated in Docker.

## Final UI Layout

## Desktop Structure

The page now uses a top-level 3-column desktop grid:

1. Left column: Watchlist grid panel (5x10 grouped rendering).
2. Middle column: Main Chart, Heatmap + P&L, Positions.
3. Right column: AI Assistant (top) + Trade Bar (bottom).

Spacing behavior:

- Single top-level `gap` controls spacing between columns 1/2 and 2/3.
- Single top-level outer `p-3` controls left and right outer margins.
- This ensures symmetric outer margins and consistent inter-column spacing.

## Trade Bar Placement + Density

- Trade Bar moved below AI Assistant to free space in middle analytics column.
- Trade Bar controls compacted for narrow right-column fit.
- Add button label shortened from `Add To Watchlist` to `Watch`.
- Ticker input is intentionally short; quantity input narrowed.

## Overflow / Width Fixes

Final overflow fixes included:

- `min-w-0` safeguards added on panel wrappers and AI chat form/input elements.
- Trade Bar switched from fixed-width large-screen grid to compact responsive two-column layout.
- Main container uses `overflow-x-clip` to prevent accidental horizontal scrollbars from intrinsic child widths.

## Watchlist Defaults + Grouping

## New 50-Ticker Default

### Tech
AAPL, MSFT, GOOGL, AMZN, NVDA, META, ORCL, CRM, ADBE, INTC

### Financials
JPM, BAC, WFC, C, GS, MS, V, MA, AXP, BLK

### Healthcare
JNJ, PFE, MRK, UNH, ABBV, LLY, TMO, ABT, DHR, BMY

### Consumer
WMT, COST, HD, MCD, NKE, SBUX, KO, PEP, DIS, NFLX

### Industrials & Energy
XOM, CVX, CAT, DE, BA, GE, RTX, UPS, UNP, HON

## DB Seeding + Migration Behavior

Schema enhancements to `watchlist`:

- `group_key`
- `group_label`
- `group_order`
- `item_order`

Startup behavior:

1. Empty watchlist: seed 50 grouped defaults.
2. Legacy unchanged 10-ticker default: migrate to 50 grouped defaults.
3. Legacy ungrouped watchlist rows (pre-group schema): migrate to 50 grouped defaults.

New user-added tickers are inserted into the `Custom` group.

## Day-Baseline Price Logic

## Backend Data Model

`PriceUpdate` now includes day-baseline fields:

- `day_baseline_price`
- `day_change`
- `day_change_percent`
- `day_direction`

Legacy tick-to-tick fields are preserved for compatibility.

## Massive Baseline Extraction

Baseline source priority:

1. Current day open (`day.open`) when > 0.
2. Previous close (`prev_day.close`) fallback.
3. Derived baseline (`price - todays_change`) fallback.

## Pre-Market Behavior

Before market open, day open may be missing/zero; previous close fallback is used.

## Chart Population Reliability Fixes

Issue addressed: selecting a watchlist ticker could show an empty chart before stream data arrived.

Fixes:

1. Backend `/api/watchlist` now always returns a price fallback (seed/default fallback).
2. Frontend bootstraps ticker history from `price || dayBaselinePrice || previousPrice`.
3. On ticker selection, frontend injects fallback single-point history when series is empty.
4. Sparkline now renders single-point series as a flat line instead of blank placeholder.

Result: clicking watchlist symbols always renders chart content.

## Watchlist Mini Charts (Reintroduction Plan)

Goal: restore per-ticker mini moving charts in the 5x10 watchlist without degrading UI responsiveness.

Implementation checklist:

1. Pass frontend `tickerHistory` into `WatchlistPanel` so each row can access its series.
2. Render a compact sparkline in each non-empty watchlist row under the ticker label.
3. Keep series bounded (existing cap: 80 points) and render mini chart with low visual overhead.
4. Use memoized row rendering to keep update work localized and predictable.
5. Preserve fixed 10-row-per-column behavior and existing add/remove/select interactions.
6. Re-run frontend unit tests and rebuild app container for runtime validation.

Status: implemented on **2026-02-13** with memoized watchlist rows and compact per-row sparklines.

## Massive API Availability Validation

Validation run date: **2026-02-13 (US pre-market morning)**.

Checks:

1. `v3/reference/tickers/{symbol}`: 50/50 symbols found.
2. `v2/snapshot/.../tickers`: 50/50 symbols returned.
3. Confirmed pre-market missing `day.open` behavior and fallback coverage.

## Testing + Verification

## Backend

- Backend test suite run in Docker.
- Result: **90 passed**.

## Frontend Unit Tests

- Vitest frontend suite updated and run.
- Result: **10 passed**.

## Playwright (Docker)

- Smoke tests updated for new layout and flows.
- Result: passing standard smoke set; real-LLM scenario remains optional/skipped unless configured.

## Operational Note

For these UI changes to appear in the local app container, rebuild is required:

- `./scripts/start_mac.sh --build`
