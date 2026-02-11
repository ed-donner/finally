# Phase 6: Frontend Foundation - Research

**Researched:** 2026-02-11
**Domain:** Next.js static export SPA with SSE real-time data, dark terminal aesthetic
**Confidence:** HIGH

## Summary

This phase creates the frontend application shell: a dark, data-dense trading terminal layout with live SSE price streaming and connection status management. The frontend is a Next.js static export served by the existing FastAPI backend. All backend APIs are already complete, including the SSE endpoint at `GET /api/stream/prices` that pushes price updates every ~500ms.

The recommended stack is Next.js 16 (latest LTS 16.1.6) with TypeScript, Tailwind CSS v4, and Zustand for state management. Next.js 16 is the current `@latest` version and `create-next-app` will install it by default. While Next.js 16 has breaking changes (async request APIs, middleware->proxy rename, Turbopack default), none affect a static export SPA -- the breaking changes are all server-side. Tailwind CSS v4 uses a CSS-first configuration model with `@theme` directives instead of `tailwind.config.js`, which is cleaner for defining the custom terminal color scheme.

**Primary recommendation:** Use `npx create-next-app@latest frontend --typescript --tailwind --eslint --app --src-dir --import-alias "@/*"`, configure `output: 'export'` in `next.config.ts`, add Zustand for SSE price state, and build a CSS Grid layout with custom dark theme colors defined via Tailwind v4's `@theme` directive.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| next | 16.1.x | React framework with static export | Current LTS, `create-next-app@latest` installs it |
| react / react-dom | 19.2.x | UI library | Bundled with Next.js 16 |
| typescript | 5.x | Type safety | Default in create-next-app |
| tailwindcss | 4.1.x | Utility-first CSS with custom theme | CSS-first config, @theme for custom colors |
| @tailwindcss/postcss | 4.1.x | PostCSS plugin for Tailwind v4 | Required for Tailwind v4 with Next.js |
| zustand | 5.0.x | Lightweight state management | ~1KB, no providers, selector-based re-renders, ideal for high-frequency SSE updates |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| postcss | 8.x | CSS processing | Peer dependency of @tailwindcss/postcss |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Zustand | React Context | Context re-renders ALL consumers on any state change -- disastrous for ~500ms price updates across multiple components. Zustand allows selector-based subscriptions. |
| Zustand | Redux Toolkit | Massive overkill for this app's state needs. More boilerplate. |
| Tailwind v4 | Tailwind v3 | v3 still works but v4 is current, create-next-app installs v4, and @theme is cleaner for custom color definitions |

**Installation (after create-next-app):**
```bash
cd frontend
npm install zustand
```

## Architecture Patterns

### Recommended Project Structure
```
frontend/
├── src/
│   ├── app/
│   │   ├── layout.tsx          # Root layout with fonts, dark class, Zustand-unrelated
│   │   ├── page.tsx            # Single page -- the trading terminal
│   │   └── globals.css         # Tailwind imports + @theme + custom CSS
│   ├── components/
│   │   ├── layout/
│   │   │   ├── Header.tsx      # Portfolio value, cash, connection status dot
│   │   │   └── TerminalGrid.tsx # CSS Grid layout with panel regions
│   │   ├── panels/
│   │   │   ├── WatchlistPanel.tsx    # Placeholder region
│   │   │   ├── ChartPanel.tsx        # Placeholder region
│   │   │   ├── PortfolioPanel.tsx    # Placeholder region
│   │   │   └── ChatPanel.tsx         # Placeholder region
│   │   └── ui/
│   │       └── ConnectionDot.tsx     # Green/yellow/red status indicator
│   ├── stores/
│   │   ├── price-store.ts     # SSE price data + connection status
│   │   └── portfolio-store.ts # Portfolio data from REST API
│   ├── hooks/
│   │   └── use-price-stream.ts # SSE EventSource hook
│   └── lib/
│       └── api.ts              # Typed fetch wrappers for /api/* endpoints
├── next.config.ts
├── postcss.config.mjs
├── tailwind.config.ts          # NOT NEEDED in Tailwind v4 (CSS-first)
├── tsconfig.json
└── package.json
```

### Pattern 1: Zustand Store for SSE Price Data
**What:** A single Zustand store holds all price data and connection status. The SSE hook writes to it; UI components subscribe to specific slices.
**When to use:** Always -- this is the core data flow pattern for the app.
**Example:**
```typescript
// src/stores/price-store.ts
import { create } from 'zustand'

type ConnectionStatus = 'connected' | 'connecting' | 'disconnected'

interface PriceUpdate {
  ticker: string
  price: number
  previous_price: number
  timestamp: number
  change: number
  change_percent: number
  direction: 'up' | 'down' | 'flat'
}

interface PriceStore {
  prices: Record<string, PriceUpdate>
  connectionStatus: ConnectionStatus
  setPrices: (prices: Record<string, PriceUpdate>) => void
  setConnectionStatus: (status: ConnectionStatus) => void
}

export const usePriceStore = create<PriceStore>()((set) => ({
  prices: {},
  connectionStatus: 'disconnected',
  setPrices: (prices) => set({ prices }),
  setConnectionStatus: (status) => set({ connectionStatus: status }),
}))
```

### Pattern 2: SSE EventSource Hook
**What:** A custom hook manages the EventSource lifecycle, parses events, and writes to the Zustand store. Uses useEffect for setup/teardown.
**When to use:** Called once in the root page/layout to establish the SSE connection.
**Example:**
```typescript
// src/hooks/use-price-stream.ts
'use client'
import { useEffect, useRef } from 'react'
import { usePriceStore } from '@/stores/price-store'

export function usePriceStream() {
  const setPrices = usePriceStore((s) => s.setPrices)
  const setConnectionStatus = usePriceStore((s) => s.setConnectionStatus)
  const eventSourceRef = useRef<EventSource | null>(null)

  useEffect(() => {
    const connect = () => {
      setConnectionStatus('connecting')
      const es = new EventSource('/api/stream/prices')
      eventSourceRef.current = es

      es.onopen = () => {
        setConnectionStatus('connected')
      }

      es.onmessage = (event) => {
        const data = JSON.parse(event.data)
        setPrices(data)
      }

      es.onerror = () => {
        setConnectionStatus('disconnected')
        // EventSource auto-reconnects; the retry: 1000 directive
        // from the server tells the browser to retry after 1 second.
        // readyState CONNECTING (0) means it is reconnecting.
        if (es.readyState === EventSource.CONNECTING) {
          setConnectionStatus('connecting')
        }
      }
    }

    connect()

    return () => {
      eventSourceRef.current?.close()
      eventSourceRef.current = null
    }
  }, [setPrices, setConnectionStatus])
}
```

### Pattern 3: CSS Grid Terminal Layout
**What:** A CSS Grid layout that divides the viewport into named regions for the trading terminal panels.
**When to use:** The main page layout.
**Example:**
```typescript
// src/components/layout/TerminalGrid.tsx
'use client'

export function TerminalGrid({ children }: { children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-12 grid-rows-[1fr_1fr] gap-px h-[calc(100vh-3rem)] bg-terminal-border">
      {children}
    </div>
  )
}

// Usage in page.tsx:
// <TerminalGrid>
//   <div className="col-span-3 row-span-2 bg-terminal-bg">Watchlist</div>
//   <div className="col-span-6 bg-terminal-bg">Chart</div>
//   <div className="col-span-3 row-span-2 bg-terminal-bg">Chat</div>
//   <div className="col-span-3 bg-terminal-bg">Portfolio Heatmap</div>
//   <div className="col-span-3 bg-terminal-bg">Positions</div>
// </TerminalGrid>
```

### Pattern 4: Tailwind v4 @theme for Custom Colors
**What:** Define all terminal colors in CSS using Tailwind v4's @theme directive. No tailwind.config.js needed.
**When to use:** In globals.css for the entire app.
**Example:**
```css
/* src/app/globals.css */
@import "tailwindcss";

@theme {
  /* Terminal backgrounds */
  --color-terminal-bg: #0d1117;
  --color-terminal-surface: #1a1a2e;
  --color-terminal-border: #2d2d44;

  /* Brand colors */
  --color-accent-yellow: #ecad0a;
  --color-brand-blue: #209dd7;
  --color-brand-purple: #753991;

  /* Semantic colors */
  --color-price-up: #22c55e;
  --color-price-down: #ef4444;
  --color-text-primary: #e6edf3;
  --color-text-secondary: #8b949e;
  --color-text-muted: #484f58;

  /* Fonts */
  --font-mono: 'JetBrains Mono', ui-monospace, monospace;
  --font-sans: 'Inter', ui-sans-serif, sans-serif;
}
```
This automatically creates utility classes like `bg-terminal-bg`, `text-accent-yellow`, `border-terminal-border`, `font-mono`, etc.

### Anti-Patterns to Avoid
- **Using React Context for price data:** Re-renders every consumer on every 500ms update. Use Zustand with selectors instead.
- **Multiple EventSource connections:** Open exactly ONE connection in the root. Components read from the shared store.
- **Hardcoded color values in components:** Always use Tailwind utilities (`bg-terminal-bg`) not inline hex values. The @theme is the single source of truth.
- **Using `next/image` with default loader:** Static exports require `images: { unoptimized: true }` in next.config.ts. Alternatively, use plain `<img>` tags since this app has no user-uploaded images.
- **Server-side features in static export:** No cookies(), headers(), Server Actions, or dynamic routes without generateStaticParams(). This is a pure client-side SPA.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| State management for SSE | Custom pub/sub or Context with reducer | Zustand store with selectors | Selector-based subscriptions prevent unnecessary re-renders at 500ms update rate |
| SSE reconnection | Custom reconnection logic with exponential backoff | Native EventSource built-in retry | The backend sends `retry: 1000` directive; EventSource handles reconnection automatically |
| Dark theme color system | CSS variables manually scattered | Tailwind v4 @theme directive | Generates utility classes automatically, single source of truth |
| CSS reset/normalization | Custom reset stylesheet | Tailwind's built-in preflight | Included automatically with `@import "tailwindcss"` |
| Font loading | Manual @font-face declarations | next/font/google | Self-hosts fonts at build time, no external requests, automatic optimization |

**Key insight:** EventSource has built-in reconnection. The server's `retry: 1000\n\n` directive tells the browser to retry after 1 second. Do NOT build custom reconnection logic -- just track readyState for the UI indicator.

## Common Pitfalls

### Pitfall 1: Static Export Forgetting images.unoptimized
**What goes wrong:** Build fails with "Image Optimization using the default loader is not compatible with `output: export`"
**Why it happens:** Next.js default image loader requires a server for on-the-fly optimization
**How to avoid:** Set `images: { unoptimized: true }` in next.config.ts, or use plain `<img>` tags
**Warning signs:** Build error mentioning Image Optimization

### Pitfall 2: Using 'use client' Incorrectly in Static Export
**What goes wrong:** Components that need browser APIs (EventSource, window) fail during build pre-rendering
**Why it happens:** Server Components are pre-rendered during `next build` even in static export mode
**How to avoid:** Mark any component using browser APIs with `'use client'` at the top. Wrap browser API access in useEffect.
**Warning signs:** "window is not defined" or "EventSource is not defined" during build

### Pitfall 3: Tailwind v4 Using Old Config File
**What goes wrong:** Custom colors don't work, utilities aren't generated
**Why it happens:** Tailwind v4 uses `@theme` in CSS, not `tailwind.config.js`. The old config file is ignored.
**How to avoid:** Define all customizations in globals.css using `@theme { }`. Use `@tailwindcss/postcss` in postcss.config.mjs (not `tailwindcss`).
**Warning signs:** Custom utility classes not being recognized, PostCSS error about using tailwindcss directly as a plugin

### Pitfall 4: EventSource Error Handler Closing Connection
**What goes wrong:** Manual close() in onerror kills the built-in auto-reconnect
**Why it happens:** Developers mistakenly call es.close() in the error handler, then try custom reconnection
**How to avoid:** In onerror, only update the connection status UI. Do NOT call close(). EventSource auto-reconnects.
**Warning signs:** SSE connection drops permanently on first transient error

### Pitfall 5: Zustand Re-render Storm
**What goes wrong:** Entire app re-renders on every 500ms price update
**Why it happens:** Components subscribe to the entire store instead of selecting specific slices
**How to avoid:** Always use selectors: `usePriceStore((s) => s.prices['AAPL'])` not `usePriceStore()`
**Warning signs:** UI feels sluggish, React DevTools shows frequent full-tree re-renders

### Pitfall 6: Next.js Static Export Output Directory
**What goes wrong:** FastAPI can't find the built frontend files
**Why it happens:** Next.js static export outputs to `out/` by default, but the backend expects files in `backend/static/`
**How to avoid:** Either configure `distDir` in next.config.ts or copy `frontend/out/*` to `backend/static/` after build. The Dockerfile should handle this in the build stage.
**Warning signs:** FastAPI returns 404 for all non-API routes

### Pitfall 7: PostCSS Config File Format
**What goes wrong:** Tailwind v4 PostCSS plugin not loaded
**Why it happens:** Using `postcss.config.js` (CJS) instead of `postcss.config.mjs` (ESM), or using `tailwindcss` instead of `@tailwindcss/postcss` as the plugin name
**How to avoid:** Use `postcss.config.mjs` with `"@tailwindcss/postcss": {}` as the plugin
**Warning signs:** Tailwind classes not being processed, error about PostCSS plugin

## Code Examples

### next.config.ts for Static Export
```typescript
// Source: https://nextjs.org/docs/app/guides/static-exports
import type { NextConfig } from 'next'

const nextConfig: NextConfig = {
  output: 'export',
  images: {
    unoptimized: true,
  },
}

export default nextConfig
```

### postcss.config.mjs
```javascript
// Source: https://tailwindcss.com/docs/installation/using-postcss
const config = {
  plugins: {
    "@tailwindcss/postcss": {},
  },
}

export default config
```

### Root Layout with Fonts
```typescript
// src/app/layout.tsx
// Source: https://nextjs.org/docs/app/getting-started/fonts
import type { Metadata } from 'next'
import { Inter, JetBrains_Mono } from 'next/font/google'
import './globals.css'

const inter = Inter({
  subsets: ['latin'],
  variable: '--font-sans',
  display: 'swap',
})

const jetbrainsMono = JetBrains_Mono({
  subsets: ['latin'],
  variable: '--font-mono',
  display: 'swap',
})

export const metadata: Metadata = {
  title: 'FinAlly - AI Trading Workstation',
  description: 'AI-powered trading workstation with live market data',
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en" className={`${inter.variable} ${jetbrainsMono.variable}`}>
      <body className="bg-terminal-bg text-text-primary font-sans antialiased">
        {children}
      </body>
    </html>
  )
}
```

### Connection Status Dot Component
```typescript
// src/components/ui/ConnectionDot.tsx
'use client'
import { usePriceStore } from '@/stores/price-store'

const statusColors = {
  connected: 'bg-green-500',
  connecting: 'bg-yellow-500 animate-pulse',
  disconnected: 'bg-red-500',
} as const

export function ConnectionDot() {
  const status = usePriceStore((s) => s.connectionStatus)

  return (
    <div className="flex items-center gap-2">
      <div className={`h-2 w-2 rounded-full ${statusColors[status]}`} />
      <span className="text-xs text-text-secondary capitalize">{status}</span>
    </div>
  )
}
```

### SSE Event Data Shape (from backend)
```typescript
// This is the shape of each SSE message's data field.
// The backend sends: data: {"AAPL": {...}, "GOOGL": {...}, ...}
// Source: backend/app/market/models.py PriceUpdate.to_dict()

interface PriceUpdate {
  ticker: string
  price: number
  previous_price: number
  timestamp: number       // Unix seconds
  change: number          // price - previous_price
  change_percent: number  // percentage change
  direction: 'up' | 'down' | 'flat'
}

// SSE data payload is Record<string, PriceUpdate>
type PricePayload = Record<string, PriceUpdate>
```

### Portfolio API Response Shape (from backend)
```typescript
// Source: backend/app/portfolio/models.py
interface PositionResponse {
  ticker: string
  quantity: number
  avg_cost: number
  current_price: number
  market_value: number
  unrealized_pnl: number
  unrealized_pnl_percent: number
}

interface PortfolioResponse {
  cash_balance: number
  positions: PositionResponse[]
  total_value: number
}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `next export` command | `output: 'export'` in next.config | Next.js 14 (2023) | Build command is just `next build`, no separate export step |
| tailwind.config.js | @theme in CSS | Tailwind v4 (Jan 2025) | No JS config file needed; CSS-first theming |
| tailwindcss as PostCSS plugin | @tailwindcss/postcss | Tailwind v4 (Jan 2025) | Separate package required in postcss.config |
| @tailwind base/components/utilities | @import "tailwindcss" | Tailwind v4 (Jan 2025) | Single import replaces three directives |
| darkMode: 'class' in config | @custom-variant dark | Tailwind v4 (Jan 2025) | Dark mode configured in CSS, not JS |
| middleware.ts | proxy.ts | Next.js 16 (2025) | N/A for static export -- neither is used |
| Webpack default bundler | Turbopack default | Next.js 16 (2025) | Faster dev/build; no config change needed |

**Deprecated/outdated:**
- `tailwind.config.js` / `tailwind.config.ts`: Still works in Tailwind v4 for backward compatibility but not the recommended approach. Use `@theme` in CSS instead.
- `next export` command: Removed in Next.js 14+. Use `output: 'export'` in config.
- `darkMode: 'class'` in Tailwind config: Replaced by `@custom-variant dark (...)` in CSS.

## Open Questions

1. **Build output destination for Docker**
   - What we know: Next.js outputs static files to `frontend/out/` by default. Backend expects files in `backend/static/` (configured via `STATIC_DIR` env var).
   - What's unclear: Whether to change Next.js `distDir` or copy files in the Dockerfile build stage.
   - Recommendation: Keep default `out/` directory. The Dockerfile (Phase 11) will copy `frontend/out/*` to the static directory. For local dev, use the Next.js dev server on a different port and proxy API calls, or build and copy manually.

2. **Local development workflow**
   - What we know: Static export means no Next.js dev server with API proxy built-in.
   - What's unclear: Exact dev workflow for frontend+backend together.
   - Recommendation: Run `next dev` on port 3000 with `rewrites` in next.config for dev mode (rewrites are only unsupported in the export output, not during dev). This allows hot reloading. Alternatively, use the backend directly with built frontend.

3. **Font loading with next/font in static export**
   - What we know: next/font/google self-hosts fonts at build time, which should work with static export since fonts become static assets.
   - What's unclear: Whether the CSS variable approach works seamlessly with Tailwind v4's `--font-*` theme variables.
   - Recommendation: Use next/font with CSS variables (`variable` prop), then reference those variables in the @theme block. Test during implementation.

## Sources

### Primary (HIGH confidence)
- Next.js static export docs: https://nextjs.org/docs/app/guides/static-exports - Full static export configuration and limitations
- Next.js 16 upgrade guide: https://nextjs.org/docs/app/guides/upgrading/version-16 - Breaking changes confirmed not to affect static export
- Tailwind CSS v4 theme docs: https://tailwindcss.com/docs/theme - @theme directive syntax and usage
- Tailwind CSS v4 dark mode docs: https://tailwindcss.com/docs/dark-mode - @custom-variant dark syntax
- Tailwind CSS Next.js installation: https://tailwindcss.com/docs/guides/nextjs - Exact installation steps and packages
- Backend source code: `backend/app/market/stream.py` - SSE event format, retry directive, payload shape
- Backend source code: `backend/app/market/models.py` - PriceUpdate.to_dict() output shape
- Backend source code: `backend/app/portfolio/models.py` - PortfolioResponse schema
- Backend source code: `backend/app/main.py` - Static file serving configuration (STATIC_DIR env var)

### Secondary (MEDIUM confidence)
- Zustand npm: https://www.npmjs.com/package/zustand - v5.0.11, ~1KB bundle, TypeScript support confirmed
- Next.js endoflife.date: https://eosl.date/eol/product/nextjs/ - Latest version 16.1.6 confirmed
- JetBrains Mono on Google Fonts: https://fonts.google.com/specimen/JetBrains+Mono - Available via next/font/google

### Tertiary (LOW confidence)
- None. All findings verified with official documentation or source code.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - Verified with official docs (Next.js, Tailwind, Zustand npm)
- Architecture: HIGH - Patterns derived from official docs + backend source code analysis
- Pitfalls: HIGH - Verified against official docs (static export limitations, Tailwind v4 migration guide, EventSource MDN spec)

**Research date:** 2026-02-11
**Valid until:** 2026-03-11 (30 days -- stack is stable, no major releases expected)
