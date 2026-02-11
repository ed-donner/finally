# Phase 8: Portfolio Visualizations & Trading - Research

**Researched:** 2026-02-11
**Domain:** Frontend data visualization (treemap, line chart, table) + trade execution UI
**Confidence:** HIGH

## Summary

Phase 8 adds the portfolio visualization and trading layer to the existing frontend shell. The current codebase has placeholder `<PortfolioPanel>` and a "Positions" text span in the grid. The backend API is fully implemented with `GET /api/portfolio`, `POST /api/portfolio/trade`, and `GET /api/portfolio/history` all returning well-defined Pydantic responses. The portfolio Zustand store exists but only tracks `cashBalance` and `totalValue` -- it needs to be extended with positions, history snapshots, and trade execution.

There are two visualization components: a **treemap heatmap** (positions sized by portfolio weight, colored by P&L) and a **P&L line/area chart** (portfolio value over time). For the treemap, the recommendation is **Recharts Treemap** with a custom `content` render function -- Recharts v3.7.0 provides a flat treemap with per-cell custom rendering via SVG, exactly matching the requirement. For the P&L chart, **lightweight-charts v5** (already installed) should be reused with an `AreaSeries`, matching the pattern already established in `ChartPanel.tsx`.

**Primary recommendation:** Use Recharts `<Treemap>` with `content` prop for the heatmap, reuse lightweight-charts `AreaSeries` for P&L chart, extend the existing `portfolio-store.ts` with positions/history/trade state, and build a simple trade bar with inline error display.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| recharts | ^3.7.0 | Treemap heatmap visualization | De facto React charting library (92.8 Context7 benchmark). Has a built-in `<Treemap>` component with `content` prop for full custom rendering. SVG-based, works in static export. |
| lightweight-charts | ^5.1.0 (already installed) | P&L area chart | Already used for the ticker chart in `ChartPanel.tsx`. Same `createChart` + `AreaSeries` pattern. No new dependency. |
| zustand | ^5.0.11 (already installed) | Trade execution state, positions, history | Already the project's state management. Extend `portfolio-store.ts`. |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| @types/d3-scale (optional) | n/a | Color interpolation for P&L gradient | Only if using d3-scale for green-to-red interpolation. A simple linear interpolation function avoids this dependency entirely. |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Recharts Treemap | d3-hierarchy + hand-rolled SVG | More control, smaller bundle, but more code to maintain. Recharts handles layout, animation, responsive container. Recommend Recharts for speed of implementation. |
| Recharts Treemap | react-canvas-treemap | Canvas-based, but obscure library with low adoption. Not recommended. |
| lightweight-charts for P&L | Recharts LineChart | Would work but adds inconsistency -- ticker chart already uses lightweight-charts. Keeping the same library for both chart types is cleaner. |

**Installation:**
```bash
cd frontend && npm install recharts
```

Note: `recharts` depends on `d3-scale`, `d3-shape`, `d3-interpolate` etc. internally. These are bundled -- no separate install needed.

## Architecture Patterns

### Recommended Component Structure
```
frontend/src/
├── components/
│   ├── panels/
│   │   └── PortfolioPanel.tsx      # Existing -- becomes container with tabs/sections
│   ├── portfolio/
│   │   ├── Heatmap.tsx             # Recharts Treemap with custom content
│   │   ├── PnlChart.tsx            # lightweight-charts AreaSeries for portfolio value
│   │   ├── PositionsTable.tsx      # HTML table of all holdings
│   │   └── TradeBar.tsx            # Ticker input, quantity input, buy/sell buttons
│   └── ...
├── stores/
│   └── portfolio-store.ts          # Extended: positions[], snapshots[], executeTrade()
└── ...
```

### Pattern 1: Recharts Treemap with Custom Content Renderer
**What:** Use the `content` prop on `<Treemap>` to render each cell as a custom SVG `<rect>` + `<text>`, with fill color derived from the position's P&L percentage.
**When to use:** When each treemap cell needs individual coloring (green/red P&L gradient) and custom labels (ticker + P&L%).

```tsx
// Source: Recharts official custom content treemap example + Context7 /recharts/recharts
import { Treemap, ResponsiveContainer, Tooltip } from 'recharts';

interface HeatmapData {
  name: string;       // ticker
  size: number;       // market_value (determines rectangle size)
  pnlPercent: number; // for color
  pnl: number;        // for label
}

function pnlColor(pnlPercent: number): string {
  // Clamp to [-10, +10] range, interpolate red-to-green
  const clamped = Math.max(-10, Math.min(10, pnlPercent));
  const t = (clamped + 10) / 20; // 0 = deep red, 1 = deep green
  const r = Math.round(239 - t * 205);
  const g = Math.round(68 + t * 129);
  const b = Math.round(68 - t * 2);
  return `rgb(${r},${g},${b})`;
}

function CustomContent(props: any) {
  const { x, y, width, height, name, pnlPercent, pnl } = props;
  if (width < 2 || height < 2) return null;
  return (
    <g>
      <rect x={x} y={y} width={width} height={height}
        fill={pnlColor(pnlPercent)} stroke="#2d2d44" strokeWidth={1} />
      {width > 40 && height > 30 && (
        <>
          <text x={x + width / 2} y={y + height / 2 - 6}
            textAnchor="middle" fill="#fff" fontSize={12} fontFamily="JetBrains Mono">
            {name}
          </text>
          <text x={x + width / 2} y={y + height / 2 + 10}
            textAnchor="middle" fill="#fff" fontSize={10} fontFamily="JetBrains Mono">
            {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)}%
          </text>
        </>
      )}
    </g>
  );
}

function Heatmap({ data }: { data: HeatmapData[] }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <Treemap
        data={data}
        dataKey="size"
        content={<CustomContent />}
        isAnimationActive={false}
        type="flat"
      >
        <Tooltip />
      </Treemap>
    </ResponsiveContainer>
  );
}
```

### Pattern 2: P&L Area Chart with lightweight-charts (reuse existing pattern)
**What:** Same `createChart` + `addSeries(AreaSeries)` pattern as `ChartPanel.tsx`, but with portfolio snapshot data from `/api/portfolio/history`.
**When to use:** For the portfolio value over time visualization.

```tsx
// Source: Context7 /tradingview/lightweight-charts + existing ChartPanel.tsx pattern
import { createChart, AreaSeries, ColorType } from "lightweight-charts";
import type { IChartApi, ISeriesApi, UTCTimestamp } from "lightweight-charts";

// In useEffect (same pattern as ChartPanel.tsx):
const chart = createChart(containerRef.current, {
  layout: {
    background: { type: ColorType.Solid, color: "#1a1a2e" },
    textColor: "#8b949e",
  },
  grid: {
    vertLines: { color: "#2d2d44" },
    horzLines: { color: "#2d2d44" },
  },
  timeScale: { borderColor: "#2d2d44", timeVisible: true },
  rightPriceScale: { borderColor: "#2d2d44" },
  width: containerRef.current.clientWidth,
  height: containerRef.current.clientHeight,
});

const series = chart.addSeries(AreaSeries, {
  lineColor: "#ecad0a",       // accent yellow
  topColor: "rgba(236, 173, 10, 0.4)",
  bottomColor: "rgba(236, 173, 10, 0.05)",
  lineWidth: 2,
});

// Data from /api/portfolio/history snapshots
series.setData(snapshots.map(s => ({
  time: (new Date(s.recorded_at).getTime() / 1000) as UTCTimestamp,
  value: s.total_value,
})));
chart.timeScale().fitContent();
```

### Pattern 3: Trade Execution with Optimistic Update + Error Handling
**What:** POST to `/api/portfolio/trade`, handle 400 errors inline, refresh portfolio state on success.
**When to use:** For the trade bar component.

```tsx
// In portfolio-store.ts:
executeTrade: async (ticker: string, side: "buy" | "sell", quantity: number) => {
  set({ tradeError: null, tradeLoading: true });
  try {
    const res = await fetch("/api/portfolio/trade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker, side, quantity }),
    });
    if (!res.ok) {
      const err = await res.json();
      set({ tradeError: err.detail, tradeLoading: false });
      return;
    }
    set({ tradeLoading: false });
    // Refresh portfolio to get updated positions
    get().fetchPortfolio();
  } catch {
    set({ tradeError: "Trade failed", tradeLoading: false });
  }
},
```

### Pattern 4: Layout Integration
**What:** The existing grid in `page.tsx` already has two cells for portfolio content in row 2:
- `col-span-3` (under the main chart) -- currently `<PortfolioPanel />`
- `col-span-3` (next to it) -- currently just "Positions" text

The heatmap + P&L chart go in the PortfolioPanel cell. The positions table + trade bar go in the adjacent cell.

```
┌─────────────┬──────────────────────┬─────────────┐
│  Watchlist   │     Chart (ticker)   │    Chat     │
│  (3 cols)    │     (6 cols)         │   (3 cols)  │
│  row-span-2  │                      │  row-span-2 │
│             ├──────────┬───────────┤             │
│             │ Heatmap  │ Positions │             │
│             │ + P&L    │ + Trade   │             │
│             │ (3 cols) │ (3 cols)  │             │
└─────────────┴──────────┴───────────┘
```

### Anti-Patterns to Avoid
- **Polling portfolio on a timer:** Don't poll `/api/portfolio` every N seconds. Instead, refetch after trades and on initial load. Prices update via SSE but portfolio positions only change on trade execution.
- **Storing derived data:** Don't store computed P&L in the store. The API already computes `unrealized_pnl` and `unrealized_pnl_percent` per position. Store the API response as-is.
- **Over-abstracting the treemap:** Don't create a generic treemap wrapper. This is a single-use visualization. Keep it simple and direct.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Treemap layout algorithm | Squarify rectangle packing from scratch | Recharts `<Treemap>` (uses d3-hierarchy internally) | Squarify is a non-trivial algorithm with edge cases around aspect ratios and very small values |
| Responsive chart sizing | Manual window resize listeners | `<ResponsiveContainer>` (Recharts) / `ResizeObserver` (lightweight-charts, already used in ChartPanel) | Both libraries handle this natively |
| P&L color gradient | Complex HSL interpolation | Simple linear RGB interpolation between red (#ef4444) and green (#22c55e) | Two-color gradient doesn't need a color library -- clamp to range, interpolate linearly |
| Currency formatting | `toFixed(2)` with manual $ prefix | `Intl.NumberFormat` (already used in `Header.tsx`) | Handles thousands separators, negative formatting, locale |

**Key insight:** The treemap layout math is the only genuinely complex piece. Everything else (table, chart, form) is standard React UI with well-established patterns.

## Common Pitfalls

### Pitfall 1: Treemap Rendering with Zero/Negative Values
**What goes wrong:** Treemap `dataKey` must be positive. If a position has zero market value or negative weight, the treemap breaks or renders nothing.
**Why it happens:** A fully sold position might still appear in the API response briefly, or a position with 0 quantity could slip through.
**How to avoid:** Filter positions to `quantity > 0` and `market_value > 0` before passing to `<Treemap>`. The API already excludes zero-quantity positions (positions are deleted on full sell), but add a defensive filter.
**Warning signs:** Empty treemap, React error about invalid data in Recharts.

### Pitfall 2: Lightweight-Charts Time Data Must Be Sorted and Ascending
**What goes wrong:** `setData()` throws or renders nothing if timestamps are not strictly ascending.
**Why it happens:** Portfolio snapshots from the API should already be sorted (`ORDER BY recorded_at ASC`), but if the frontend processes them incorrectly or timestamps have duplicates.
**How to avoid:** The backend already sorts. Trust the API order. Convert `recorded_at` (ISO string) to Unix seconds. Avoid deduplication unless errors occur.
**Warning signs:** Console error from lightweight-charts about non-ascending time values.

### Pitfall 3: Trade Form Not Clearing Error on New Submission
**What goes wrong:** Previous error message persists after a new (successful) trade, confusing the user.
**Why it happens:** Error state not cleared at the start of `executeTrade`.
**How to avoid:** Always set `tradeError: null` at the beginning of `executeTrade` before the fetch.
**Warning signs:** Stale red error text below trade bar after a successful trade.

### Pitfall 4: Portfolio Not Refreshing After Trade
**What goes wrong:** User executes a trade but the positions table and heatmap still show old data.
**Why it happens:** Trade endpoint returns the trade result, not the full portfolio. Must call `fetchPortfolio()` after a successful trade to update positions.
**How to avoid:** Chain `fetchPortfolio()` after successful `executeTrade()` in the store action. Also fetch history snapshots to update the P&L chart.
**Warning signs:** Stale position quantities or cash balance after trading.

### Pitfall 5: Recharts Treemap Animation Causing Stutter
**What goes wrong:** Default animation on Recharts Treemap causes visual stutter when data changes (e.g., after a trade).
**Why it happens:** Recharts v3 has animation enabled by default. Treemap animation between different data sets can be janky.
**How to avoid:** Set `isAnimationActive={false}` on the `<Treemap>`. The terminal aesthetic doesn't need smooth treemap transitions.
**Warning signs:** Flickering or morphing rectangles on data refresh.

### Pitfall 6: Empty State When No Positions Exist
**What goes wrong:** Treemap/table renders blank or errors when the user has no positions (initial state: $10k cash, no holdings).
**Why it happens:** `positions` array is empty on fresh start.
**How to avoid:** Show an explicit empty state message: "No positions yet. Use the trade bar to buy your first stock." Guard both treemap and table with `positions.length > 0` check.
**Warning signs:** Blank white space or React error in portfolio panel.

## Code Examples

### Portfolio Store Extension
```tsx
// Source: Existing portfolio-store.ts pattern + backend API contract
interface Position {
  ticker: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_pnl_percent: number;
}

interface Snapshot {
  total_value: number;
  recorded_at: string;
}

interface PortfolioStore {
  cashBalance: number;
  totalValue: number;
  positions: Position[];
  snapshots: Snapshot[];
  loading: boolean;
  tradeLoading: boolean;
  tradeError: string | null;
  fetchPortfolio: () => Promise<void>;
  fetchHistory: () => Promise<void>;
  executeTrade: (ticker: string, side: "buy" | "sell", quantity: number) => Promise<void>;
}
```

### Positions Table Pattern
```tsx
// Source: Standard React table with Tailwind styling matching existing components
function PositionsTable({ positions }: { positions: Position[] }) {
  if (positions.length === 0) {
    return (
      <div className="flex items-center justify-center h-full">
        <span className="text-text-muted font-mono text-xs">No positions</span>
      </div>
    );
  }
  return (
    <div className="overflow-y-auto">
      <table className="w-full font-mono text-xs">
        <thead>
          <tr className="text-text-muted uppercase tracking-wider">
            <th className="text-left py-1 px-2">Ticker</th>
            <th className="text-right py-1 px-2">Qty</th>
            <th className="text-right py-1 px-2">Avg Cost</th>
            <th className="text-right py-1 px-2">Price</th>
            <th className="text-right py-1 px-2">P&L</th>
            <th className="text-right py-1 px-2">%</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => (
            <tr key={p.ticker} className="border-t border-terminal-border">
              <td className="py-1 px-2 text-text-primary font-bold">{p.ticker}</td>
              <td className="py-1 px-2 text-right text-text-secondary">{p.quantity}</td>
              <td className="py-1 px-2 text-right text-text-secondary">${p.avg_cost.toFixed(2)}</td>
              <td className="py-1 px-2 text-right text-text-primary">${p.current_price.toFixed(2)}</td>
              <td className={`py-1 px-2 text-right ${p.unrealized_pnl >= 0 ? 'text-price-up' : 'text-price-down'}`}>
                {p.unrealized_pnl >= 0 ? '+' : ''}${p.unrealized_pnl.toFixed(2)}
              </td>
              <td className={`py-1 px-2 text-right ${p.unrealized_pnl_percent >= 0 ? 'text-price-up' : 'text-price-down'}`}>
                {p.unrealized_pnl_percent >= 0 ? '+' : ''}{p.unrealized_pnl_percent.toFixed(2)}%
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
```

### Trade Bar Pattern
```tsx
// Source: Standard form pattern matching existing WatchlistPanel input styling
function TradeBar() {
  const [ticker, setTicker] = useState("");
  const [quantity, setQuantity] = useState("");
  const executeTrade = usePortfolioStore((s) => s.executeTrade);
  const tradeError = usePortfolioStore((s) => s.tradeError);
  const tradeLoading = usePortfolioStore((s) => s.tradeLoading);

  const handleTrade = (side: "buy" | "sell") => {
    const qty = parseFloat(quantity);
    if (!ticker.trim() || isNaN(qty) || qty <= 0) return;
    executeTrade(ticker.toUpperCase().trim(), side, qty);
  };

  return (
    <div className="p-2 border-t border-terminal-border">
      <div className="flex items-center gap-1">
        <input value={ticker} onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder="TICKER" className="bg-terminal-bg border border-terminal-border rounded px-2 py-1 font-mono text-xs w-20 ..." />
        <input value={quantity} onChange={(e) => setQuantity(e.target.value)}
          placeholder="QTY" type="number" className="bg-terminal-bg border border-terminal-border rounded px-2 py-1 font-mono text-xs w-16 ..." />
        <button onClick={() => handleTrade("buy")} disabled={tradeLoading}
          className="bg-price-up/20 text-price-up border border-price-up/30 rounded px-2 py-1 font-mono text-xs hover:bg-price-up/30">
          BUY
        </button>
        <button onClick={() => handleTrade("sell")} disabled={tradeLoading}
          className="bg-price-down/20 text-price-down border border-price-down/30 rounded px-2 py-1 font-mono text-xs hover:bg-price-down/30">
          SELL
        </button>
      </div>
      {tradeError && (
        <p className="text-price-down text-xs font-mono mt-1">{tradeError}</p>
      )}
    </div>
  );
}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Recharts v2 `Cell` for per-element colors | Recharts v3 `content` prop (custom render function) | Recharts 3.0 (2024) | `Cell` is deprecated in v3, will be removed in v4. Use `content` prop instead. |
| lightweight-charts `addAreaSeries()` | lightweight-charts v5 `addSeries(AreaSeries, opts)` | v5.0 (2024) | New API uses generic `addSeries()` with series type as first argument. Already used correctly in `ChartPanel.tsx`. |
| Recharts v2 required `ResponsiveContainer` wrapper always | Recharts v3 still requires it but has better SSR handling | 3.0 | Set `isAnimationActive={false}` or `"auto"` in SSR/static export environments. |

**Deprecated/outdated:**
- `Cell` component in Recharts: Deprecated in v3, use `content` prop on chart components instead.
- `chart.addAreaSeries()` in lightweight-charts: Old v3/v4 API. Use `chart.addSeries(AreaSeries, options)` in v5.
- `chart.addLineSeries()` in lightweight-charts: Old API. Use `chart.addSeries(LineSeries, options)` in v5.

## Open Questions

1. **Heatmap and P&L chart sharing the PortfolioPanel space**
   - What we know: The grid gives 3 columns for the PortfolioPanel. Both the heatmap and P&L chart need to fit here.
   - What's unclear: Whether to stack them vertically (heatmap top, P&L bottom) or use tabs. Vertical stacking with roughly 50/50 split is simpler and shows both at once.
   - Recommendation: Stack vertically -- heatmap top half, P&L chart bottom half. Both benefit from being always visible.

2. **How often to refetch portfolio history for P&L chart**
   - What we know: Backend records snapshots every 30 seconds and after each trade. API returns all snapshots chronologically.
   - What's unclear: Whether to refetch history periodically or only on trade.
   - Recommendation: Fetch on initial load and after each trade. The P&L chart doesn't need real-time updates between trades -- snapshots accumulate server-side regardless.

3. **Trade bar pre-filling ticker from watchlist selection**
   - What we know: `selectedTicker` exists in the watchlist store.
   - What's unclear: Whether the trade bar should auto-fill the ticker from the selected watchlist item.
   - Recommendation: Yes, pre-fill from `selectedTicker` for better UX. User can still type a different ticker.

## Sources

### Primary (HIGH confidence)
- Context7 `/recharts/recharts` - Treemap component API, custom content rendering, Cell deprecation
- Context7 `/tradingview/lightweight-charts` - AreaSeries v5 API, addSeries pattern, chart options
- Existing codebase `ChartPanel.tsx` - Verified lightweight-charts v5 usage pattern with createChart, LineSeries, ResizeObserver
- Existing codebase `portfolio-store.ts` - Current store shape, fetchPortfolio implementation
- Existing codebase `backend/app/portfolio/models.py` - Exact API response schemas (PortfolioResponse, TradeResponse, PortfolioHistoryResponse)
- Existing codebase `backend/app/routes/portfolio.py` - API endpoint definitions and error handling (400 for validation errors)

### Secondary (MEDIUM confidence)
- [Recharts Treemap API docs](https://recharts.github.io/en-US/api/Treemap/) - content prop, colorPanel, type="flat"
- [Recharts Custom Content Treemap example](https://recharts.github.io/en-US/examples/CustomContentTreemap/) - CustomizedContent render function pattern
- [Recharts npm](https://www.npmjs.com/package/recharts) - Latest version 3.7.0
- [Recharts 3.0 migration guide](https://github.com/recharts/recharts/wiki/3.0-migration-guide) - Cell deprecation notice
- [React Graph Gallery treemap](https://www.react-graph-gallery.com/treemap) - d3-hierarchy treemap layout pattern for reference

### Tertiary (LOW confidence)
- None. All findings verified against primary or secondary sources.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - Recharts confirmed via Context7 + npm; lightweight-charts already in use and verified
- Architecture: HIGH - Following exact patterns from existing ChartPanel.tsx and existing store conventions
- Pitfalls: HIGH - Derived from actual API contracts and Recharts v3 documented behavior

**Research date:** 2026-02-11
**Valid until:** 2026-03-11 (stable libraries, no breaking changes expected)
