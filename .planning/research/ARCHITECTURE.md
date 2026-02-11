# Architecture Research

**Domain:** AI Trading Workstation (single-container, real-time, LLM-integrated)
**Researched:** 2026-02-11
**Confidence:** HIGH

## System Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│  Docker Container (port 8000)                                        │
│                                                                      │
│  ┌─────────────────── FastAPI Application ──────────────────────┐    │
│  │                                                               │    │
│  │  Lifespan Context Manager                                     │    │
│  │  ├── startup: init DB, create PriceCache, start MarketData    │    │
│  │  └── shutdown: stop MarketData, close DB                      │    │
│  │                                                               │    │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐         │    │
│  │  │ Stream   │ │Portfolio │ │Watchlist │ │  Chat    │         │    │
│  │  │ Router   │ │ Router   │ │ Router   │ │ Router   │         │    │
│  │  │ (SSE)    │ │ (REST)   │ │ (REST)   │ │ (REST)   │         │    │
│  │  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘         │    │
│  │       │             │            │             │               │    │
│  │  ┌────┴─────┐ ┌─────┴────────────┴──┐   ┌─────┴─────┐        │    │
│  │  │ Price    │ │   Portfolio Service  │   │   LLM     │        │    │
│  │  │ Cache    │ │   (trade, valuation) │   │  Service  │        │    │
│  │  └────┬─────┘ └─────────┬───────────┘   └─────┬─────┘        │    │
│  │       │                 │                      │              │    │
│  │  ┌────┴──────────┐ ┌───┴──────────┐   ┌───────┴───────┐      │    │
│  │  │ MarketData    │ │   Database   │   │  LiteLLM /    │      │    │
│  │  │ Source (sim/  │ │  (aiosqlite) │   │  OpenRouter   │      │    │
│  │  │  massive)     │ │              │   │               │      │    │
│  │  └───────────────┘ └───────┬──────┘   └───────────────┘      │    │
│  │                            │                                  │    │
│  └────────────────────────────┼──────────────────────────────────┘    │
│                               │                                      │
│  ┌────────────────────────────┴──────────────────────────────────┐    │
│  │  SQLite (db/finally.db)                                       │    │
│  └───────────────────────────────────────────────────────────────┘    │
│                                                                      │
│  ┌───────────────────────────────────────────────────────────────┐    │
│  │  Static Files (Next.js export in /static)                     │    │
│  └───────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘

Browser ←── SSE (EventSource) ──→ /api/stream/prices
Browser ←── REST (fetch)      ──→ /api/portfolio, /api/watchlist, /api/chat
Browser ←── Static files      ──→ /* (Next.js HTML/JS/CSS)
```

### Component Responsibilities

| Component | Responsibility | Communicates With |
|-----------|---------------|-------------------|
| **FastAPI Lifespan** | Initialize/tear down all shared resources (DB, PriceCache, MarketDataSource) | All components (creates them) |
| **PriceCache** (exists) | In-memory latest prices, thread-safe reads | MarketDataSource writes; SSE Router, Portfolio Service read |
| **MarketDataSource** (exists) | Produce price updates on a background task | PriceCache (write-only) |
| **SSE Stream Router** (exists) | Push price updates to browser via EventSource | PriceCache (read-only) |
| **Database Layer** (new) | Async SQLite access, schema init, CRUD for all tables | Portfolio Service, Watchlist Router, Chat Router, Snapshot Task |
| **Portfolio Service** (new) | Trade execution, P&L calculation, portfolio valuation | Database Layer, PriceCache |
| **Portfolio Router** (new) | REST API for portfolio reads and trade execution | Portfolio Service |
| **Watchlist Router** (new) | REST API for watchlist CRUD | Database Layer, MarketDataSource (add/remove tickers) |
| **Chat Router** (new) | REST API for LLM chat | LLM Service, Portfolio Service, Watchlist Router |
| **LLM Service** (new) | System prompt construction, LiteLLM call, structured output parsing | LiteLLM (external), Portfolio Service (context), Database Layer (chat history) |
| **Snapshot Task** (new) | Background task recording portfolio value every 30s | Database Layer, PriceCache |
| **Next.js Frontend** (new) | Entire browser UI, SSE connection, API calls, visualizations | FastAPI (same origin /api/*, /api/stream/*) |

## Recommended Project Structure

### Backend

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app creation, lifespan, router mounting
│   ├── dependencies.py         # Shared state (cache, db, source) via app.state
│   ├── market/                 # [EXISTS] Market data subsystem
│   │   ├── __init__.py
│   │   ├── models.py           # PriceUpdate dataclass
│   │   ├── cache.py            # PriceCache
│   │   ├── interface.py        # MarketDataSource ABC
│   │   ├── simulator.py        # GBMSimulator + SimulatorDataSource
│   │   ├── massive_client.py   # MassiveDataSource
│   │   ├── factory.py          # create_market_data_source()
│   │   ├── seed_prices.py      # Seed data for simulator
│   │   └── stream.py           # SSE streaming router
│   ├── db/                     # [NEW] Database layer
│   │   ├── __init__.py
│   │   ├── connection.py       # aiosqlite connection management
│   │   ├── schema.py           # CREATE TABLE statements, lazy init
│   │   └── seed.py             # Default data (user, watchlist)
│   ├── portfolio/              # [NEW] Portfolio domain
│   │   ├── __init__.py
│   │   ├── service.py          # Trade execution, P&L calc, valuation
│   │   └── router.py           # /api/portfolio endpoints
│   ├── watchlist/              # [NEW] Watchlist domain
│   │   ├── __init__.py
│   │   └── router.py           # /api/watchlist endpoints
│   ├── chat/                   # [NEW] LLM chat domain
│   │   ├── __init__.py
│   │   ├── service.py          # Prompt construction, LLM call, action execution
│   │   └── router.py           # /api/chat endpoint
│   └── snapshots.py            # [NEW] Background task for portfolio_snapshots
├── tests/
│   ├── conftest.py
│   ├── market/                 # [EXISTS]
│   ├── db/                     # [NEW]
│   ├── portfolio/              # [NEW]
│   ├── watchlist/              # [NEW]
│   └── chat/                   # [NEW]
├── pyproject.toml
└── uv.lock
```

### Frontend

```
frontend/
├── src/
│   ├── app/                    # Next.js App Router
│   │   ├── layout.tsx          # Root layout (dark theme, global styles)
│   │   └── page.tsx            # Single page — the trading workstation
│   ├── components/             # UI components
│   │   ├── Watchlist.tsx       # Ticker grid with sparklines, price flash
│   │   ├── Chart.tsx           # Main chart area (Lightweight Charts)
│   │   ├── Portfolio.tsx       # Heatmap (treemap) of positions
│   │   ├── PnlChart.tsx        # Portfolio value over time
│   │   ├── Positions.tsx       # Positions table
│   │   ├── TradeBar.tsx        # Trade input (ticker, qty, buy/sell)
│   │   ├── Chat.tsx            # AI chat panel
│   │   └── Header.tsx          # Portfolio value, cash, connection status
│   ├── stores/                 # Zustand state management
│   │   ├── priceStore.ts       # SSE price data, sparkline history
│   │   ├── portfolioStore.ts   # Positions, cash, P&L (fetched via REST)
│   │   └── chatStore.ts        # Chat messages, loading state
│   ├── hooks/                  # Custom React hooks
│   │   └── useSSE.ts           # EventSource connection management
│   └── lib/                    # Utilities
│       ├── api.ts              # Typed fetch wrappers for /api/*
│       └── format.ts           # Number/currency formatting
├── next.config.ts              # output: 'export'
├── tailwind.config.ts
├── tsconfig.json
└── package.json
```

### Structure Rationale

- **`app/market/`:** Already exists and is complete. All new code integrates with it, never modifies it.
- **`app/db/`:** Isolated database layer. All SQL lives here. Other modules call functions, never write raw SQL.
- **`app/portfolio/`, `app/watchlist/`, `app/chat/`:** Domain-organized modules. Each has a router (API surface) and optionally a service (business logic). Keeps modules short and focused.
- **`app/main.py`:** The assembly point. Creates the FastAPI app, wires the lifespan, mounts all routers. This is the only file that knows about all the components.
- **`stores/`:** Zustand stores separate concerns. The price store is write-heavy (SSE updates every 500ms). Portfolio and chat stores are write-on-demand (REST responses).

## Architectural Patterns

### Pattern 1: FastAPI Lifespan for Resource Management

**What:** Use the `@asynccontextmanager` lifespan to create, wire, and tear down all shared resources (PriceCache, MarketDataSource, DB connection, background tasks). Attach them to `app.state` so routers can access them via `request.app.state`.

**When to use:** Always. This is the modern FastAPI pattern replacing deprecated `on_startup`/`on_shutdown`.

**Confidence:** HIGH (verified via Context7/FastAPI official docs)

**Example:**
```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.market import PriceCache, create_market_data_source, create_stream_router
from app.db.connection import get_db, init_db
from app.db.seed import seed_default_data

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    # 1. Database
    db = await init_db("db/finally.db")
    await seed_default_data(db)

    # 2. Market data
    cache = PriceCache()
    source = create_market_data_source(cache)

    # 3. Load watchlist from DB, start market data
    watchlist_tickers = await load_watchlist_tickers(db)
    await source.start(watchlist_tickers)

    # 4. Start background snapshot task
    snapshot_task = asyncio.create_task(snapshot_loop(db, cache))

    # 5. Store on app.state for routers
    app.state.db = db
    app.state.price_cache = cache
    app.state.market_source = source

    yield

    # --- Shutdown ---
    snapshot_task.cancel()
    await source.stop()
    await db.close()

app = FastAPI(lifespan=lifespan)
app.include_router(create_stream_router(app.state.price_cache))
# Note: routers that need state access it via request.app.state
```

**Trade-offs:** All initialization in one place makes the startup sequence clear but the lifespan function can get long. Keep it as a wiring layer -- call functions, don't put logic inline.

### Pattern 2: aiosqlite with Connection-Per-Request

**What:** Use `aiosqlite` for non-blocking SQLite access. Maintain a single shared connection opened at startup, closed at shutdown. SQLite is single-writer anyway, so connection pooling adds complexity for no benefit in a single-user app.

**When to use:** Single-user SQLite applications. For multi-user or high concurrency, switch to PostgreSQL with asyncpg and connection pooling.

**Confidence:** HIGH (aiosqlite is the standard async SQLite library for Python; single-connection pattern matches SQLite's single-writer architecture)

**Example:**
```python
import aiosqlite

async def init_db(db_path: str) -> aiosqlite.Connection:
    """Open the database, create tables if missing, return the connection."""
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row  # dict-like access to rows
    await db.execute("PRAGMA journal_mode=WAL")  # Better concurrent read perf
    await db.execute("PRAGMA foreign_keys=ON")
    await _create_tables(db)
    return db

async def _create_tables(db: aiosqlite.Connection):
    """Lazy init: CREATE TABLE IF NOT EXISTS for all tables."""
    await db.executescript(SCHEMA_SQL)
    await db.commit()
```

**Trade-offs:** Single connection means writes are serialized, which is fine for a single-user demo. WAL mode allows concurrent reads while a write is happening.

### Pattern 3: Zustand Stores with SSE Event Feeding

**What:** A custom `useSSE` hook manages the `EventSource` connection and feeds price updates directly into a Zustand store. Components subscribe to fine-grained slices of state to minimize re-renders. Sparkline history accumulates in the store (capped at N points).

**When to use:** Any real-time frontend where a persistent server connection feeds into client state.

**Confidence:** HIGH (Zustand is the standard lightweight state management for React; verified via Context7)

**Example:**
```typescript
// stores/priceStore.ts
import { create } from 'zustand'

interface PriceData {
  ticker: string
  price: number
  previous_price: number
  direction: 'up' | 'down' | 'flat'
  change_percent: number
  timestamp: number
}

interface PriceState {
  prices: Record<string, PriceData>
  sparklines: Record<string, number[]>  // last N prices per ticker
  updatePrices: (data: Record<string, PriceData>) => void
}

const MAX_SPARKLINE_POINTS = 60

export const usePriceStore = create<PriceState>()((set) => ({
  prices: {},
  sparklines: {},
  updatePrices: (data) =>
    set((state) => {
      const sparklines = { ...state.sparklines }
      for (const [ticker, update] of Object.entries(data)) {
        const existing = sparklines[ticker] || []
        sparklines[ticker] = [...existing, update.price].slice(-MAX_SPARKLINE_POINTS)
      }
      return { prices: data, sparklines }
    }),
}))

// hooks/useSSE.ts
import { useEffect, useRef } from 'react'
import { usePriceStore } from '@/stores/priceStore'

export function useSSE() {
  const updatePrices = usePriceStore((s) => s.updatePrices)
  const esRef = useRef<EventSource | null>(null)

  useEffect(() => {
    const es = new EventSource('/api/stream/prices')
    esRef.current = es

    es.onmessage = (event) => {
      const data = JSON.parse(event.data)
      updatePrices(data)
    }

    return () => es.close()
  }, [updatePrices])
}
```

**Trade-offs:** Zustand is lightweight and fast. The price store updates every 500ms. Using selectors (`usePriceStore(s => s.prices[ticker])`) ensures only affected components re-render. Sparkline array growth is capped.

### Pattern 4: LLM Structured Output with Auto-Execution

**What:** LiteLLM calls OpenRouter with `response_format: { type: "json_schema", json_schema: {...} }` to get structured JSON responses. The backend parses the response, auto-executes trades and watchlist changes, then returns the complete result to the frontend.

**When to use:** When the LLM needs to both communicate and take actions. Structured output eliminates brittle regex parsing.

**Confidence:** HIGH (verified via Context7/LiteLLM docs; json_schema response_format is the recommended approach)

**Example:**
```python
from litellm import completion
import json

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "trading_response",
        "schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "trades": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ticker": {"type": "string"},
                            "side": {"type": "string", "enum": ["buy", "sell"]},
                            "quantity": {"type": "number"}
                        },
                        "required": ["ticker", "side", "quantity"]
                    }
                },
                "watchlist_changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ticker": {"type": "string"},
                            "action": {"type": "string", "enum": ["add", "remove"]}
                        },
                        "required": ["ticker", "action"]
                    }
                }
            },
            "required": ["message"],
            "additionalProperties": False
        },
        "strict": True
    }
}

async def chat(user_message: str, portfolio_context: dict, history: list) -> dict:
    messages = build_messages(portfolio_context, history, user_message)
    response = await asyncio.to_thread(
        completion,
        model="openrouter/openai/gpt-oss-120b",
        messages=messages,
        response_format=RESPONSE_SCHEMA,
    )
    return json.loads(response.choices[0].message.content)
```

**Trade-offs:** Structured output is not streamed token-by-token. The PLAN explicitly states this is fine because Cerebras inference is fast enough. A loading indicator in the UI bridges the wait.

### Pattern 5: Static Next.js Export Served by FastAPI

**What:** Next.js builds a fully static export (`output: 'export'` in `next.config.ts`). The Docker build copies the `out/` directory into the Python image. FastAPI serves it via `StaticFiles` mount at the root, with API routes taking priority.

**When to use:** Single-container deployments where the frontend is a client-side SPA. No SSR needed.

**Confidence:** HIGH (verified via Context7/Next.js docs; static export is a first-class feature of the App Router)

**Example:**
```python
# In main.py, after all API routers are mounted:
from fastapi.staticfiles import StaticFiles

# API routers are mounted first (they take priority)
app.include_router(stream_router)
app.include_router(portfolio_router, prefix="/api")
app.include_router(watchlist_router, prefix="/api")
app.include_router(chat_router, prefix="/api")

# Static files last — catches everything else
app.mount("/", StaticFiles(directory="static", html=True), name="static")
```

**Trade-offs:** No server-side rendering. All components are client components. This is fine for a data-dense dashboard where every widget needs real-time updates anyway.

## Data Flow

### Price Data Flow (Real-Time)

```
MarketDataSource (sim or Massive)
    │ writes every 500ms
    ▼
PriceCache (in-memory, thread-safe)
    │ read every 500ms
    ▼
SSE Stream Router ──── text/event-stream ────→ Browser EventSource
                                                     │
                                                     ▼
                                              Zustand priceStore
                                                     │
                                          ┌──────────┼──────────┐
                                          ▼          ▼          ▼
                                     Watchlist    Chart    Header
                                     (flash +   (candle/  (total
                                     sparkline)  line)    value)
```

### Trade Execution Flow

```
User clicks Buy/Sell (TradeBar)  ──OR──  LLM returns trade in structured output
         │                                          │
         ▼                                          ▼
    POST /api/portfolio/trade              chat service auto-executes
         │                                          │
         └──────────────┬───────────────────────────┘
                        ▼
               Portfolio Service
               ├── Validate (cash check / share check)
               ├── Get current price from PriceCache
               ├── Update positions table (DB)
               ├── Update cash balance (DB)
               ├── Insert trade record (DB)
               └── Record portfolio snapshot (DB)
                        │
                        ▼
               Return result to caller
                        │
                        ▼
               Frontend fetches updated portfolio
```

### Chat Flow

```
User types message (Chat component)
         │
    POST /api/chat { message: "..." }
         │
         ▼
    Chat Service
    ├── Load portfolio context (Portfolio Service + PriceCache)
    ├── Load recent chat history (DB)
    ├── Build messages array with system prompt
    ├── Call LiteLLM (openrouter/openai/gpt-oss-120b)
    ├── Parse structured JSON response
    ├── Auto-execute trades (Portfolio Service)
    ├── Auto-execute watchlist changes (DB + MarketDataSource)
    ├── Store messages + actions (DB)
    └── Return complete response
         │
         ▼
    Frontend renders:
    ├── Assistant message text
    ├── Trade confirmations (if any)
    └── Watchlist change confirmations (if any)
```

### Portfolio Snapshot Flow (Background)

```
Background Task (every 30 seconds)
    │
    ├── Read all positions from DB
    ├── Get current prices from PriceCache
    ├── Calculate total value (cash + sum(qty * price))
    └── Insert portfolio_snapshots row (DB)
```

### Key Data Flow Rules

1. **PriceCache is read-only for all consumers.** Only MarketDataSource writes to it.
2. **Database is the source of truth for portfolio state.** PriceCache is the source of truth for current prices. Portfolio value = DB positions valued at PriceCache prices.
3. **All REST endpoints are synchronous request/response.** No streaming for REST. Only SSE streams.
4. **LLM responses are not streamed.** The full structured JSON is returned as a single response.
5. **Frontend fetches portfolio state after mutations.** After a trade, the frontend re-fetches `/api/portfolio` to get the updated state rather than trying to compute it locally.

## Build Order and Dependencies

The dependency graph determines what must be built first.

```
Phase 1: Database Layer
    └── No dependencies on new code. Can be built and tested standalone.

Phase 2: Portfolio & Watchlist
    └── Depends on: Database Layer (Phase 1) + PriceCache (exists)
    └── Watchlist also depends on MarketDataSource (exists) for add/remove

Phase 3: LLM Chat
    └── Depends on: Portfolio Service (Phase 2) + Database Layer (Phase 1)
    └── Can be initially built with mock mode, then real LLM

Phase 4: App Assembly (main.py + lifespan)
    └── Depends on: All backend components
    └── Wires everything together, mounts routers

Phase 5: Frontend
    └── Depends on: All API endpoints working (Phase 4)
    └── Can start in parallel once API contracts are defined
    └── SSE endpoint already exists; REST endpoints from Phase 2-3

Phase 6: Docker & Integration
    └── Depends on: Frontend build + Backend complete
    └── Multi-stage Dockerfile, scripts, E2E tests
```

### Build Order Rationale

- **Database first** because every other new component depends on it (portfolio, watchlist, chat all need persistence).
- **Portfolio and Watchlist before Chat** because the LLM service calls the portfolio service and watchlist operations. You cannot build chat without trade execution.
- **App assembly (main.py)** can be started early as a skeleton and grow incrementally, but the full lifespan wiring needs all components.
- **Frontend can overlap with backend phases** once the API contract (endpoint shapes) is defined. Use mock/hardcoded data initially.
- **Docker last** because it needs both a built frontend and a working backend.

## Anti-Patterns

### Anti-Pattern 1: Global Mutable State

**What people do:** Store the PriceCache, DB connection, or MarketDataSource as module-level globals.
**Why it's wrong:** Makes testing painful (can't isolate), makes the dependency graph implicit, breaks if you ever run multiple app instances.
**Do this instead:** Create resources in the lifespan, attach to `app.state`, access via `request.app.state` in route handlers.

### Anti-Pattern 2: ORM for Simple SQLite

**What people do:** Reach for SQLAlchemy ORM with models, sessions, and migration tooling for a 6-table single-user SQLite database.
**Why it's wrong:** Massive overhead for a simple schema. ORM async support adds another layer (async sessions, greenlet bridging). The schema is static (lazy init, not migrations).
**Do this instead:** Use `aiosqlite` directly with raw SQL. The schema is small enough that raw SQL is clearer and faster. Wrap in thin async functions that return dicts or dataclasses.

### Anti-Pattern 3: Streaming LLM Tokens for Chat

**What people do:** Implement SSE or WebSocket streaming for LLM responses to show tokens appearing one by one.
**Why it's wrong for this project:** Structured output requires the full response before parsing. Cerebras inference is fast enough (~1-2s). Streaming adds complexity to both backend (async generator) and frontend (incremental JSON parsing) for minimal UX benefit.
**Do this instead:** Show a loading indicator, return the complete structured JSON, render it all at once.

### Anti-Pattern 4: Computed Portfolio State on the Frontend

**What people do:** After a trade, try to update portfolio state locally in the frontend by adjusting cash and positions.
**Why it's wrong:** Creates two sources of truth (frontend computed state vs. database). Race conditions with concurrent actions (e.g., LLM-initiated trade + manual trade). P&L calculations are tricky with average cost basis.
**Do this instead:** After any mutation (trade, watchlist change), re-fetch the relevant API endpoint. The backend is the single source of truth.

### Anti-Pattern 5: Multiple SQLite Connections

**What people do:** Open a new `aiosqlite.connect()` per request, or create a connection pool.
**Why it's wrong:** SQLite supports only one writer at a time. Multiple connections add locking overhead and complexity. Connection pooling is a PostgreSQL pattern that doesn't apply here.
**Do this instead:** One connection, opened at startup, closed at shutdown. SQLite with WAL mode handles concurrent reads fine.

## Integration Points

### External Services

| Service | Integration Pattern | Notes |
|---------|---------------------|-------|
| OpenRouter (LLM) | `litellm.completion()` via `asyncio.to_thread()` | LiteLLM's completion is synchronous; run in thread. Use `response_format` for structured output. Model: `openrouter/openai/gpt-oss-120b`. |
| Massive / Polygon.io | `massive.RESTClient` via `asyncio.to_thread()` | Already implemented. Synchronous REST client run in thread. |

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| Market subsystem <-> Portfolio | PriceCache (read-only) | Portfolio service reads current prices for valuation and trade execution. Never modifies PriceCache. |
| Watchlist <-> Market subsystem | `MarketDataSource.add_ticker()` / `remove_ticker()` | Watchlist changes propagate to the data source so new tickers get prices. |
| Chat <-> Portfolio | `PortfolioService` function calls | Chat service calls portfolio service to execute trades and get context. Direct Python calls, not HTTP. |
| Chat <-> Watchlist | Database writes + `MarketDataSource` calls | Chat service modifies watchlist in DB and notifies the market source. |
| Frontend <-> Backend | HTTP (REST + SSE), same origin | No CORS. All `/api/*` routes. Static files served from `/`. |

## Sources

- [FastAPI Lifespan Events](https://fastapi.tiangolo.com/advanced/events/) - HIGH confidence (Context7 + official docs)
- [LiteLLM Structured Output / JSON Schema](https://docs.litellm.ai/docs/completion/json_mode) - HIGH confidence (Context7 + official docs)
- [Next.js Static Export](https://nextjs.org/docs/app/building-your-application/deploying/static-exports) - HIGH confidence (Context7 + official docs)
- [Zustand State Management](https://zustand.docs.pmnd.rs/) - HIGH confidence (Context7)
- [aiosqlite](https://github.com/omnilib/aiosqlite) - HIGH confidence (standard async SQLite library for Python)
- [TradingView Lightweight Charts](https://tradingview.github.io/lightweight-charts/) - HIGH confidence (Context7)

---
*Architecture research for: FinAlly AI Trading Workstation*
*Researched: 2026-02-11*
