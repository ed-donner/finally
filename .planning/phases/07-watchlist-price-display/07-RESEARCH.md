# Phase 7: Watchlist & Price Display - Research

**Researched:** 2026-02-11
**Domain:** React frontend -- real-time price grid, sparkline charts, canvas financial charts, CSS animations
**Confidence:** HIGH

## Summary

Phase 7 transforms the placeholder watchlist and chart panels into a live, interactive trading terminal experience. The work divides into four technical areas: (1) a watchlist grid displaying live prices from the existing Zustand price store, (2) CSS flash animations on price changes, (3) inline SVG sparkline mini-charts, and (4) a full-size price chart using TradingView's Lightweight Charts v5.

The existing frontend from Phase 6 provides a solid foundation: the Zustand price store already receives SSE price data as `Record<string, PriceUpdate>`, the CSS Grid layout has placeholder panels for watchlist and chart, and Tailwind v4 is configured with terminal colors including `price-up` (#22c55e) and `price-down` (#ef4444). The primary new dependency is `lightweight-charts` v5.1.0 (ESM-only, canvas-based, designed for financial data). Sparklines should be hand-rolled as simple SVG `<polyline>` elements -- no library needed for this.

**Primary recommendation:** Use lightweight-charts v5.1.0 directly with a React useRef/useEffect pattern (no wrapper library needed). Hand-roll SVG sparklines. Extend the Zustand price store to accumulate price history per ticker. Use CSS animation keyframes with a toggle key for flash effects.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| lightweight-charts | 5.1.0 | Canvas-based financial charting for main chart area | TradingView official library; performant canvas rendering; designed for streaming data; v5 is latest with ESM and modern API |
| zustand | 5.0.11 | State management (already installed) | Already in use; needs extension for price history and watchlist state |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| (hand-rolled SVG) | n/a | Sparkline mini-charts | SVG polyline is ~15 lines of code; no library needed |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| lightweight-charts direct | lightweight-charts-react-wrapper (2.1.1) | Wrapper adds abstraction but hides the chart API; direct useRef pattern gives full control and is the officially documented approach |
| Hand-rolled SVG sparkline | react-sparklines (1.7.0) | Library is 9 years old, adds dependency for a trivial SVG; hand-rolling is simpler and more customizable |
| Hand-rolled SVG sparkline | recharts Sparkline | Recharts is heavyweight (200KB+); overkill for a tiny line |

**Installation:**
```bash
cd frontend && npm install lightweight-charts@5.1.0
```

## Architecture Patterns

### Recommended New File Structure
```
src/
├── stores/
│   ├── price-store.ts         # MODIFY: add priceHistory, accumulation logic
│   └── watchlist-store.ts     # NEW: watchlist CRUD state, selectedTicker
├── components/
│   ├── panels/
│   │   ├── WatchlistPanel.tsx  # REPLACE: full watchlist grid implementation
│   │   └── ChartPanel.tsx     # REPLACE: lightweight-charts integration
│   └── ui/
│       ├── Sparkline.tsx      # NEW: SVG sparkline component
│       └── PriceCell.tsx      # NEW: single ticker row with flash animation
```

### Pattern 1: Price History Accumulation in Zustand Store

**What:** Extend the price store to keep a rolling history of prices per ticker for sparklines and the main chart. The SSE stream sends the full current price snapshot; on each update, append the new price to a per-ticker array.

**When to use:** Every time SSE data arrives (via `setPrices` in the existing hook).

**Example:**
```typescript
// In price-store.ts
interface PriceStore {
  prices: Record<string, PriceUpdate>;
  priceHistory: Record<string, { time: number; value: number }[]>;
  connectionStatus: ConnectionStatus;
  setPrices: (prices: Record<string, PriceUpdate>) => void;
  setConnectionStatus: (status: ConnectionStatus) => void;
}

// In setPrices action:
setPrices: (incoming) => set((state) => {
  const newHistory = { ...state.priceHistory };
  for (const [ticker, update] of Object.entries(incoming)) {
    const existing = newHistory[ticker] || [];
    newHistory[ticker] = [
      ...existing,
      { time: update.timestamp, value: update.price },
    ];
  }
  return { prices: incoming, priceHistory: newHistory };
}),
```

**Key detail:** The SSE sends updates every ~500ms. Over a 30-minute session, each ticker accumulates ~3,600 data points. This is well within memory limits and fine for chart performance.

### Pattern 2: Lightweight Charts v5 with useRef/useEffect

**What:** Create and manage the chart imperatively using refs, not a wrapper component. The chart instance lives in a ref, is created in useEffect, and data is pushed via the `update()` method on the series.

**When to use:** For the main ChartPanel component.

**Example:**
```typescript
// Source: TradingView official React tutorial
// https://tradingview.github.io/lightweight-charts/tutorials/react/simple
import { createChart, LineSeries, ColorType } from 'lightweight-charts';

function ChartPanel() {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ReturnType<typeof createChart>>(null);
  const seriesRef = useRef<ReturnType<typeof chart.addSeries>>(null);

  // Create chart on mount
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#1a1a2e' },
        textColor: '#8b949e',
      },
      grid: {
        vertLines: { color: '#2d2d44' },
        horzLines: { color: '#2d2d44' },
      },
      width: containerRef.current.clientWidth,
      height: containerRef.current.clientHeight,
    });
    const series = chart.addSeries(LineSeries, {
      color: '#209dd7',
      lineWidth: 2,
    });
    chartRef.current = chart;
    seriesRef.current = series;

    // Responsive resize
    const observer = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      chart.applyOptions({ width, height });
    });
    observer.observe(containerRef.current);

    return () => {
      observer.disconnect();
      chart.remove();
    };
  }, []);

  // Push data updates from price history
  // ...use seriesRef.current.update({ time, value }) for real-time points

  return <div ref={containerRef} className="h-full w-full" />;
}
```

**CRITICAL v5 API note:** In lightweight-charts v5, you MUST use `chart.addSeries(LineSeries, options)` instead of the v4 `chart.addLineSeries(options)`. The series type constructors (`LineSeries`, `AreaSeries`, etc.) are imported directly from `'lightweight-charts'`.

### Pattern 3: CSS Flash Animation with Key Toggle

**What:** On each price update, briefly flash the row background green (uptick) or red (downtick) with a ~500ms fade back to transparent.

**When to use:** Each PriceCell/ticker row when a new price arrives with direction !== 'flat'.

**Example:**
```css
/* In globals.css */
@keyframes flash-up {
  0% { background-color: rgba(34, 197, 94, 0.3); }
  100% { background-color: transparent; }
}
@keyframes flash-down {
  0% { background-color: rgba(239, 68, 68, 0.3); }
  100% { background-color: transparent; }
}
.animate-flash-up {
  animation: flash-up 500ms ease-out;
}
.animate-flash-down {
  animation: flash-down 500ms ease-out;
}
```

**React re-trigger trick:** CSS animations only replay when the element is re-added to the DOM or the animation-name changes. Use a React `key` on a wrapper div that changes each time a price updates (e.g., `key={ticker + timestamp}`). This forces React to unmount/remount the animated wrapper, retriggering the animation.

```typescript
// In PriceCell.tsx
const flashClass =
  direction === 'up' ? 'animate-flash-up' :
  direction === 'down' ? 'animate-flash-down' : '';

return (
  <div key={`${ticker}-${timestamp}`} className={flashClass}>
    {/* price content */}
  </div>
);
```

### Pattern 4: Hand-Rolled SVG Sparkline

**What:** A tiny SVG component that renders a polyline from an array of price values.

**When to use:** Inline beside each ticker in the watchlist grid.

**Example:**
```typescript
// Source: https://alexplescan.com/posts/2023/07/08/easy-svg-sparklines/
interface SparklineProps {
  data: number[];
  width?: number;
  height?: number;
  color?: string;
}

function Sparkline({ data, width = 80, height = 24, color = '#209dd7' }: SparklineProps) {
  if (data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;

  const points = data
    .map((v, i) => {
      const x = (i / (data.length - 1)) * width;
      const y = height - ((v - min) / range) * height;
      return `${x},${y}`;
    })
    .join(' ');

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      <polyline
        points={points}
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}
```

### Pattern 5: Watchlist Store for CRUD and Selection

**What:** A separate Zustand store for watchlist state: the list of tickers (fetched from `/api/watchlist`), selected ticker, and add/remove actions.

**When to use:** WatchlistPanel, ChartPanel (reads selectedTicker), and any add/remove UI.

**Example:**
```typescript
interface WatchlistStore {
  tickers: string[];
  selectedTicker: string | null;
  loading: boolean;
  fetchWatchlist: () => Promise<void>;
  addTicker: (ticker: string) => Promise<void>;
  removeTicker: (ticker: string) => Promise<void>;
  selectTicker: (ticker: string) => void;
}
```

### Anti-Patterns to Avoid
- **Re-creating chart on every render:** The chart must be created once in useEffect with an empty dependency array. Use `seriesRef.current.update()` for data changes, never `setData()` on every tick.
- **Storing chart instance in state:** Chart refs must be React refs (`useRef`), not state. Putting the chart in state triggers re-renders.
- **Using setData() for streaming updates:** `setData()` replaces all data and causes a full redraw. Use `update()` for appending individual data points in real-time.
- **Unbounded price history arrays:** Cap history at ~5000 points per ticker to prevent memory growth. With updates every 500ms, this is ~40 minutes of data.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Financial charting with time axis, crosshair, price scale | Custom canvas chart | lightweight-charts v5 | Time axis formatting, auto-scaling, crosshair, zoom, pan are extremely complex |
| Chart resize handling | Manual window resize listener | ResizeObserver | ResizeObserver responds to container size changes (not just window), essential in CSS Grid layouts |
| Price flash re-trigger | Manual DOM manipulation / requestAnimationFrame | React key prop change | React's reconciler handles DOM removal/re-insertion cleanly |

**Key insight:** The chart library handles all the hard parts (canvas rendering, time axis, price formatting, crosshair interaction, zoom/pan). Everything else (sparklines, flash animations, watchlist grid) is simple enough to build with React primitives.

## Common Pitfalls

### Pitfall 1: Lightweight Charts in Next.js Static Export
**What goes wrong:** `createChart` accesses `window` and `document`, which don't exist during SSR/static generation.
**Why it happens:** Next.js pre-renders pages, even with `output: 'export'`.
**How to avoid:** The ChartPanel is already `"use client"`. Ensure the chart is created only inside `useEffect` (which only runs client-side), never at module scope or during render. If needed, use dynamic import: `const { createChart } = await import('lightweight-charts')`.
**Warning signs:** "window is not defined" or "document is not defined" errors during `npm run build`.

### Pitfall 2: lightweight-charts v5 API Changes
**What goes wrong:** Using v4 API patterns like `chart.addLineSeries()` which don't exist in v5.
**Why it happens:** Most tutorials and examples online target v3/v4. v5 was released relatively recently.
**How to avoid:** Always import series types from the package: `import { LineSeries, AreaSeries } from 'lightweight-charts'`. Use `chart.addSeries(LineSeries, options)`.
**Warning signs:** "chart.addLineSeries is not a function" runtime error.

### Pitfall 3: CSS Animation Not Retriggering
**What goes wrong:** The flash animation plays once and then never again, even when the price keeps updating.
**Why it happens:** CSS animations only run when first applied. Re-applying the same class to the same element does nothing.
**How to avoid:** Use React's `key` prop tied to the price timestamp. When the key changes, React unmounts and remounts the element, retriggering the animation.
**Warning signs:** Flash works on first price update but never again.

### Pitfall 4: Excessive Re-renders from Zustand Price Store
**What goes wrong:** Every component re-renders on every price tick (~2x per second), causing jank.
**Why it happens:** Subscribing to the entire `prices` object means any ticker update triggers re-render of all subscribers.
**How to avoid:** Use granular selectors. Each PriceCell should select only its own ticker's data: `usePriceStore((s) => s.prices[ticker])`. The watchlist panel should select only the ticker list, not the price data. Use `useShallow` for object equality if selecting a subset.
**Warning signs:** React DevTools shows excessive renders; UI feels sluggish.

### Pitfall 5: Chart Container Height Zero
**What goes wrong:** Lightweight Charts renders a blank/invisible chart.
**Why it happens:** The chart container div has no explicit height or its parent uses `flex/grid` but the container collapses to 0.
**How to avoid:** Ensure the chart container has `h-full w-full` AND its parent chain has explicit height. Use ResizeObserver to dynamically set chart dimensions. The CSS Grid layout from Phase 6 already handles parent sizing.
**Warning signs:** Chart container exists in DOM but has `height: 0` in DevTools.

### Pitfall 6: Time Format for Lightweight Charts
**What goes wrong:** Chart displays no data or throws errors about invalid time values.
**Why it happens:** Lightweight Charts expects time as Unix seconds (not milliseconds). The backend SSE sends `timestamp` as `time.time()` which is Unix seconds -- this is correct. But if accidentally multiplied or using `Date.now()` (which returns milliseconds), it breaks.
**How to avoid:** Pass `timestamp` from PriceUpdate directly as the `time` field in chart data. Do NOT divide or multiply. Verify with `console.log` that time values are ~1.7 billion (seconds), not ~1.7 trillion (milliseconds).
**Warning signs:** Chart shows dates from year 55,000 or similar absurd values.

## Code Examples

Verified patterns from official sources:

### Lightweight Charts v5 Dark Theme Setup
```typescript
// Source: https://tradingview.github.io/lightweight-charts/tutorials/customization/chart-colors
import { createChart, LineSeries, ColorType } from 'lightweight-charts';

const chart = createChart(container, {
  layout: {
    background: { type: ColorType.Solid, color: '#1a1a2e' },
    textColor: '#8b949e',
  },
  grid: {
    vertLines: { color: '#2d2d44' },
    horzLines: { color: '#2d2d44' },
  },
  timeScale: {
    borderColor: '#2d2d44',
    timeVisible: true,
    secondsVisible: false,
  },
  rightPriceScale: {
    borderColor: '#2d2d44',
  },
});

const series = chart.addSeries(LineSeries, {
  color: '#209dd7',
  lineWidth: 2,
});
```

### Real-time Data Update with update()
```typescript
// Source: https://tradingview.github.io/lightweight-charts/docs/intro
// Use update() to add new point or update last point
series.update({ time: 1707600000, value: 192.50 });

// setData() only for initial load of historical data
series.setData(historicalPoints);
```

### Watchlist API Contract (from existing backend)
```typescript
// GET /api/watchlist response shape:
interface WatchlistResponse {
  items: Array<{
    ticker: string;
    added_at: string;
    price: number | null;
    change: number | null;
    change_percent: number | null;
    direction: string | null; // 'up' | 'down' | 'flat'
  }>;
  count: number;
}

// POST /api/watchlist - body: { ticker: string }
// DELETE /api/watchlist/{ticker}
```

### SSE Data Shape (from existing backend)
```typescript
// Each SSE event data is a Record<string, PriceUpdate>:
{
  "AAPL": {
    "ticker": "AAPL",
    "price": 190.50,
    "previous_price": 190.25,
    "timestamp": 1707600000.123,
    "change": 0.25,
    "change_percent": 0.1314,
    "direction": "up"
  },
  "GOOGL": { ... },
  // ... all tracked tickers
}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `chart.addLineSeries()` | `chart.addSeries(LineSeries)` | lightweight-charts v5 (2024) | Must import series types as values and pass to addSeries() |
| CommonJS `require()` | ESM `import` only | lightweight-charts v5 (2024) | Library is ESM-only; Next.js handles this fine with static export |
| `chart.resize(w, h)` manual | ResizeObserver + `chart.applyOptions({width, height})` | Best practice 2024+ | More reliable in flex/grid layouts than window resize listener |
| react-sparklines library | Hand-rolled SVG polyline | Ongoing trend | react-sparklines hasn't been updated since 2017; SVG polyline is ~15 lines |

**Deprecated/outdated:**
- `chart.addLineSeries()`, `chart.addAreaSeries()`, etc.: Removed in v5. Use `chart.addSeries(LineSeries)`.
- `chart.resize(width, height)`: Still works but `applyOptions` is preferred for consistency.
- CommonJS imports: lightweight-charts v5 is ESM-only.

## Open Questions

1. **Sparkline data point limit**
   - What we know: SSE sends updates every ~500ms, so sparklines accumulate ~2 points/second
   - What's unclear: The ideal max data points for sparkline rendering performance and visual clarity
   - Recommendation: Cap at 100-200 points per sparkline (covers ~1-2 minutes of data). Older points slide off the left. This keeps the SVG rendering fast and the sparkline visually readable.

2. **Chart time scale format for intraday streaming data**
   - What we know: lightweight-charts supports Unix timestamps as numbers for time values
   - What's unclear: Whether `timeVisible: true` with `secondsVisible: false` gives a good UX for sub-minute streaming data
   - Recommendation: Use `timeVisible: true, secondsVisible: true` since data updates every 500ms. Test and adjust.

## Sources

### Primary (HIGH confidence)
- `/tradingview/lightweight-charts` (Context7) - chart creation, series API, dark theme, real-time update pattern
- TradingView official React tutorial: https://tradingview.github.io/lightweight-charts/tutorials/react/simple
- TradingView v4-to-v5 migration: https://tradingview.github.io/lightweight-charts/docs/migrations/from-v4-to-v5
- `/pmndrs/zustand` (Context7) - store patterns, immer middleware, selectors
- Existing codebase: `frontend/src/stores/price-store.ts`, `backend/app/market/stream.py`, `backend/app/watchlist/router.py`

### Secondary (MEDIUM confidence)
- SVG sparkline technique: https://alexplescan.com/posts/2023/07/08/easy-svg-sparklines/
- CSS flash animation pattern: https://gist.github.com/jasonmerecki/984b5d132077e7a370567c130c1922d9
- npm version checks: `npm view lightweight-charts version` -> 5.1.0, `npm view lightweight-charts-react-wrapper version` -> 2.1.1

### Tertiary (LOW confidence)
- None. All findings verified with primary or secondary sources.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - lightweight-charts v5 API verified via Context7 and official docs; version confirmed via npm
- Architecture: HIGH - patterns verified against official React tutorial and existing codebase; SSE data shape confirmed from backend source code
- Pitfalls: HIGH - v5 API changes documented in official migration guide; flash animation retriggering is a well-known CSS behavior

**Research date:** 2026-02-11
**Valid until:** 2026-04-11 (lightweight-charts stable; Zustand stable; patterns well-established)
