# Architecture

**Analysis Date:** 2026-02-11

## Pattern Overview

**Overall:** Layered architecture with pluggable market data sources, SSE streaming, and database initialization on startup.

**Key Characteristics:**
- **Abstraction-first design** — `MarketDataSource` interface enables swapping simulator for real API without downstream changes
- **Background task-based data flow** — Market data source runs async loop writing to thread-safe cache, all reads pull from cache
- **Factory pattern for configuration** — Environment variables select between simulator and Massive API at runtime
- **Single-user in-memory state** — No inter-process communication; all state lives in Python process or SQLite
- **Thread-safe concurrent access** — PriceCache uses locks for thread-safe reads/writes from multiple coroutines

## Layers

**Presentation Layer:**
- Purpose: Serve frontend static files and handle API endpoints
- Location: Frontend compiled to `backend/` static directory; API routes to be implemented in `app/routes/`
- Contains: FastAPI route handlers, SSE streaming endpoint (`app/market/stream.py`)
- Depends on: Market data (cache), Database (portfolio/chat), LLM service
- Used by: Browser via HTTP/SSE

**Market Data Layer:**
- Purpose: Provide real-time price updates via pluggable source
- Location: `backend/app/market/`
- Contains: `PriceCache` (thread-safe in-memory store), `MarketDataSource` interface, `SimulatorDataSource` (GBM-based), `MassiveDataSource` (API-based), factory selection logic
- Depends on: numpy (for GBM math), requests (for Massive API), asyncio (for background tasks)
- Used by: SSE streaming, portfolio valuation, trade execution validation

**Portfolio Layer:**
- Purpose: Track positions, cash, trades, and portfolio snapshots
- Location: `backend/app/routes/` (to be implemented) and `backend/app/db/` (models)
- Contains: Trade execution logic, P&L calculation, position management
- Depends on: Market data layer (current prices), Database layer (persistence)
- Used by: API endpoints, LLM for trade validation and portfolio context

**LLM Integration Layer:**
- Purpose: Chat interface, trade intent parsing, structured output handling
- Location: `backend/app/llm/` (skeleton, to be implemented)
- Contains: LiteLLM client setup, structured output schema parsing, auto-execution of trades
- Depends on: OpenRouter API (Cerebras), Portfolio layer (trade execution, P&L context), Chat history (database)
- Used by: Chat endpoint

**Data Persistence Layer:**
- Purpose: SQLite database for user profile, positions, trades, watchlist, chat history
- Location: Database file: `db/finally.db` (runtime, volume-mounted); Schema/init: `backend/app/db/` (to be implemented)
- Contains: Schema definitions, seed data, lazy initialization logic
- Depends on: sqlite3 standard library
- Used by: All layers that need state persistence

## Data Flow

**Price Update Cycle (continuous):**

1. Market data source (simulator or Massive poller) runs as async background task
2. Calls `step()` or polls API, receives new prices
3. Writes to `PriceCache.update(ticker, price)` — thread-safe
4. Cache increments version counter (signals SSE to flush)
5. SSE endpoint detects version change, reads all prices from cache, serializes to JSON
6. Sends to connected browser clients via text/event-stream

**Trade Execution Flow (on user/LLM action):**

1. User submits or LLM generates trade: `{ticker, quantity, side}`
2. Route handler validates: sufficient cash (buy) or sufficient shares (sell)
3. Fetches current price from `PriceCache.get_price(ticker)`
4. Updates position in database: quantity +/- traded amount, avg_cost adjusted
5. Records trade in trades table (append-only log)
6. Updates cash_balance in users_profile
7. Records portfolio snapshot with new total_value
8. Returns confirmation to client

**Chat Flow (user message → LLM response → auto-execution):**

1. User sends message via chat endpoint
2. Load portfolio context: cash, positions with P&L, watchlist with live prices, total value
3. Load recent chat history from database
4. Send to LLM (via LiteLLM + OpenRouter/Cerebras) with structured output schema
5. Parse JSON response: message, trades[], watchlist_changes[]
6. Auto-execute each trade through trade validation pipeline (above)
7. Apply watchlist changes to database
8. Store complete interaction in chat_messages table with executed actions
9. Return message + action summaries to frontend

**State Management:**

- **Transient:** Price data — lives only in `PriceCache`, refreshed every 500ms, not persisted
- **Volatile:** User session (current client connection) — handled by SSE state (client-side)
- **Persistent:** Positions, trades, watchlist, chat history, portfolio snapshots — SQLite
- **User state:** Single hardcoded user `"default"` (future multi-user ready via user_id column)

## Key Abstractions

**PriceCache:**
- Purpose: Thread-safe, in-memory snapshot of latest prices for all tracked tickers
- Examples: `backend/app/market/cache.py`
- Pattern: Read-write lock for concurrent access, version counter for change detection
- Methods: `update()`, `get()`, `get_all()`, `remove()`, `get_price()`

**MarketDataSource:**
- Purpose: Abstract contract for price producers (simulator or API)
- Examples: `backend/app/market/interface.py`
- Pattern: Abstract base class with lifecycle (start → add_ticker/remove_ticker → stop)
- Implementations: `SimulatorDataSource` (GBM loop), `MassiveDataSource` (API poller)

**PriceUpdate:**
- Purpose: Immutable dataclass representing a single ticker's price at a moment
- Examples: `backend/app/market/models.py`
- Pattern: Frozen dataclass with computed properties (change, change_percent, direction)
- Used by: Cache value type, SSE serialization, frontend consumption

**GBMSimulator:**
- Purpose: Pure price generation using geometric Brownian motion with correlated ticker moves
- Examples: `backend/app/market/simulator.py` (lines 28–198)
- Pattern: Stateful simulator with correlation matrix (Cholesky decomposition), hot-path `step()` method
- Dependencies: Configurable drift/volatility per ticker, inter-ticker correlations

**Factory (create_market_data_source):**
- Purpose: Select between SimulatorDataSource and MassiveDataSource at runtime
- Examples: `backend/app/market/factory.py`
- Pattern: Environment variable driven selection (`MASSIVE_API_KEY`)

## Entry Points

**Application Startup:**
- Location: `backend/main.py` (to be implemented, or as part of conftest/demo)
- Triggers: Docker container `CMD`, or direct `uvicorn app:app`
- Responsibilities: FastAPI app initialization, database lazy init, market data source creation, start background tasks, mount static frontend, register routes

**SSE Stream Endpoint:**
- Location: `backend/app/market/stream.py` — `create_stream_router()` factory returns `APIRouter` with `/api/stream/prices`
- Triggers: Browser `new EventSource('/api/stream/prices')`
- Responsibilities: Detect price cache version changes, serialize all prices, push SSE events every ~500ms, detect client disconnect

**Market Data Lifecycle:**
- Location: `backend/app/market/simulator.py` — `SimulatorDataSource._run_loop()`
- Triggers: `await source.start(tickers)` → spawns asyncio task
- Responsibilities: Infinite loop: call `GBMSimulator.step()`, write prices to cache, sleep

## Error Handling

**Strategy:** Fail-safe background tasks, error logged but loop continues. API endpoints return appropriate HTTP status codes.

**Patterns:**
- Market data source steps that raise → logged as exception, loop sleeps and retries
- SSE stream client disconnect → graceful exit on `is_disconnected()`
- Trade validation failure → return error response with explanation, LLM can retry/adjust
- Database operations → SQLAlchemy/sqlite3 errors propagate to route handler, return 400/500

## Cross-Cutting Concerns

**Logging:** All modules use Python `logging` module, logger named after module (`__name__`). Info level for startup/lifecycle, debug for event details, exception for errors.

**Validation:** Trade execution validates cash/share availability before updating database. API endpoints validate input types (Pydantic for FastAPI routes). LLM structured output validated against schema.

**Authentication:** Not implemented — single hardcoded `user_id="default"`. All database queries filter by this ID (future multi-user ready).

---

*Architecture analysis: 2026-02-11*
