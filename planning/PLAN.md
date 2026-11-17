# FinAlly — AI Trading Workstation

## Project Specification

## 1. Vision

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

## 2. User Experience

### First Launch

The user runs a single Docker command (or a provided start script). A browser opens to `http://localhost:8000`. No login, no signup. They immediately see:

- A watchlist of 10 default tickers with live-updating prices in a grid
- $10,000 in virtual cash
- A dark, data-rich trading terminal aesthetic
- An AI chat panel ready to assist

### What the User Can Do

- **Watch prices stream** — prices flash green (uptick) or red (downtick) with subtle CSS animations that fade
- **View sparkline mini-charts** — price action beside each ticker in the watchlist, accumulated on the frontend from the SSE stream since page load (sparklines fill in progressively)
- **Click a ticker** to see a larger detailed chart in the main chart area
- **Buy and sell shares** — market orders only, instant fill at current price, no fees, no confirmation dialog
- **Monitor their portfolio** — a heatmap (treemap) showing positions sized by weight and colored by P&L, plus a P&L chart tracking total portfolio value over time
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, % change
- **Chat with the AI assistant** — ask about their portfolio, get analysis, and have the AI execute trades and manage the watchlist through natural language
- **Manage the watchlist** — add/remove tickers manually or via the AI chat

### Visual Design

- **Dark theme**: backgrounds around `#0d1117` or `#1a1a2e`, muted gray borders, no pure black
- **Price flash animations**: brief green/red background highlight on price change, fading over ~500ms via CSS transitions
- **Connection status indicator**: a small colored dot (green = connected, yellow = reconnecting, red = disconnected) visible in the header
- **Professional, data-dense layout**: inspired by Bloomberg/trading terminals — every pixel earns its place
- **Responsive but desktop-first**: optimized for wide screens, functional on tablet

### Color Scheme
- Accent Yellow: `#ecad0a`
- Blue Primary: `#209dd7`
- Purple Secondary: `#753991` (submit buttons)

## 3. Architecture Overview

### Single Container, Single Port

```
┌─────────────────────────────────────────────────┐
│  Docker Container (port 8000)                   │
│                                                 │
│  FastAPI (Python/uv)                            │
│  ├── /api/*          REST endpoints             │
│  ├── /api/stream/*   SSE streaming              │
│  └── /*              Static file serving         │
│                      (Next.js export)            │
│                                                 │
│  SQLite database (volume-mounted)               │
│  Background task: market data polling/sim        │
└─────────────────────────────────────────────────┘
```

- **Frontend**: Next.js with TypeScript, built as a static export (`output: 'export'`), served by FastAPI as static files
- **Backend**: FastAPI (Python), managed as a `uv` project
- **Database**: SQLite, single file at `db/finally.db`, volume-mounted for persistence
- **Real-time data**: Server-Sent Events (SSE) — simpler than WebSockets, one-way server→client push, works everywhere
- **AI integration**: LiteLLM → OpenRouter (Cerebras for fast inference), with structured outputs for trade execution
- **Market data**: Environment-variable driven — simulator by default, real data via Massive API if key provided

### Why These Choices

| Decision | Rationale |
|---|---|
| SSE over WebSockets | One-way push is all we need; simpler, no bidirectional complexity, universal browser support |
| Static Next.js export | Single origin, no CORS issues, one port, one container, simple deployment |
| SQLite over Postgres | No auth = no multi-user = no need for a database server; self-contained, zero config |
| Single Docker container | Students run one command; no docker-compose for production, no service orchestration |
| uv for Python | Fast, modern Python project management; reproducible lockfile; what students should learn |
| Market orders only | Eliminates order book, limit order logic, partial fills — dramatically simpler portfolio math |

---

## 4. Directory Structure

```
finally/
├── frontend/                 # Next.js TypeScript project (static export)
├── backend/                  # FastAPI uv project (Python)
│   └── app/
│       ├── market/           # Market data subsystem (complete; see Task 0 in §6)
│       └── db/               # Schema definitions, seed data, DB initialization
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start_mac.sh          # Launch Docker container (macOS/Linux)
│   ├── stop_mac.sh           # Stop Docker container (macOS/Linux)
│   ├── start_windows.ps1     # Launch Docker container (Windows PowerShell)
│   └── stop_windows.ps1      # Stop Docker container (Windows PowerShell)
├── test/                     # Playwright E2E tests + docker-compose.test.yml
├── db/                       # Volume mount target (SQLite file lives here at runtime)
│   └── .gitkeep              # Directory exists in repo; finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── docker-compose.yml        # Optional convenience wrapper
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. Dependencies are installed via `uv sync --extra dev` (and must include `litellm`, `pydantic`, `python-dotenv`, plus dev dependency `httpx` alongside `fastapi`, `uvicorn[standard]`, `numpy`, `massive`, and `rich`). It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/app/db/`** contains schema SQL definitions and seed logic, inside the `app` package (matching `pyproject.toml`'s `packages = ["app"]` and sitting next to `backend/app/market/`). It must **not** live at `backend/db/`: in the container that path would become `/app/db`, which the SQLite volume mount hides at runtime. The backend initializes the database during the **FastAPI lifespan startup** — creating tables, seeding default data, and recording an initial portfolio snapshot if the SQLite file doesn't exist or is empty (no per-request lazy checks).
- **`db/`** at the top level (`/app/db` in the container) is the runtime volume mount point (containing `db/.gitkeep`) and holds **only** the SQLite file, never code. The SQLite file location is controlled by `DB_PATH` (defaulting to `<repo_root>/db/finally.db` locally or `/app/db/finally.db` in Docker); the backend creates the parent directory on startup if it is missing. Required repo hygiene files (`db/.gitkeep`, `.env.example`, and `.gitignore` entries for `db/finally.db`, `frontend/node_modules/`, `frontend/.next/`, and `frontend/out/`) must be maintained.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests and supporting infrastructure (e.g., `docker-compose.test.yml`). Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- **`scripts/`** contains start/stop scripts that wrap Docker commands.

---

## 5. Environment Variables

```bash
# Required: OpenRouter API key for LLM chat functionality
OPENROUTER_API_KEY=your-openrouter-api-key-here

# Optional: Massive (Polygon.io) API key for real market data
# If not set, the built-in market simulator is used (recommended for most users)
MASSIVE_API_KEY=

# Optional: Set to "true" for deterministic mock LLM responses (testing)
LLM_MOCK=false

# Optional: Path to SQLite database file (defaults to <repo_root>/db/finally.db or /app/db/finally.db)
DB_PATH=

# Optional locally, REQUIRED for any network/cloud deployment unless LLM_MOCK=true:
# shared secret that protects the app (see §11 Security)
APP_PASSWORD=
```

### Behavior

- `.env.example` is committed to the project root as a template; `.env` is gitignored and loaded locally via `python-dotenv` or into the container via Docker `--env-file`
- If `MASSIVE_API_KEY` is set and non-empty → backend uses Massive REST API for market data
- If `MASSIVE_API_KEY` is absent or empty → backend uses the built-in market simulator
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses (for E2E tests)
- If `DB_PATH` is set and non-empty → backend uses that SQLite file path; otherwise defaults to `<repo_root>/db/finally.db` (or `/app/db/finally.db` inside the container)
- If `APP_PASSWORD` is set and non-empty → every `/api/*` request must present it (see §11 Security); if empty, no check is performed (acceptable only when bound to localhost)

---

## 6. Market Data

### Two Implementations, One Interface

Both the simulator and the Massive client implement the same abstract interface (`MarketDataSource`). The backend selects which to use based on the environment variable. All downstream code (SSE streaming, price cache, frontend) is agnostic to the source.

### Ticker Normalization & Tracked Set

- **The API layer is the authority for ticker validation.** Every request model normalizes tickers with `strip().upper()` and validates them against `^[A-Z]{1,5}$` (e.g. a Pydantic `BeforeValidator` on a shared `Ticker` type), so invalid tickers fail with `422`. Data sources only ever receive already-normalized tickers.
- As defence in depth, both `SimulatorDataSource` and `MassiveDataSource` should also normalize with `strip().upper()` in `start()`, `add_ticker()`, and `remove_ticker()`. **This is not yet true of the code. See "Known Gaps (Task 0)" below.**
- `MarketDataSource` tracks the union of **watchlist ∪ positions**. A ticker is only removed from `MarketDataSource` (and `PriceCache`) when it is present in neither `watchlist` nor `positions`.

### Simulator (Default)

- Generates prices using geometric Brownian motion (GBM) with configurable drift and volatility per ticker
- Updates at ~500ms intervals
- Correlated moves across tickers (e.g., tech stocks move together)
- Occasional random "events" — sudden 2-5% moves on a ticker for drama
- Starts from realistic seed prices (e.g., AAPL ~$190, GOOGL ~$175, etc.) and seeds newly added unknown tickers in the $50–$300 range immediately upon `add_ticker()`
- Runs as an in-process background task — no external dependencies

### Massive API (Optional)

- REST API polling (not WebSocket) — simpler, works on all tiers
- Polls for the union of all tracked tickers (`watchlist ∪ positions`) on a fixed 15-second default interval (`poll_interval=15.0`, compatible with the free tier limit of 5 calls/min)
- Parses REST response into the same format as the simulator

### Shared Price Cache

- A single background task (simulator or Massive poller) writes to an in-memory price cache (`PriceCache`)
- The cache holds the latest price, previous price, and timestamp for each ticker
- `PriceCache.remove()` must increment `_version` so SSE streams push an updated snapshot when a ticker is removed (*Task 0: not yet implemented*)
- SSE streams read from this cache and push updates to connected clients
- This architecture supports future multi-user scenarios without changes to the data layer

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- Sends an initial `retry: 1000\n\n` directive; client handles reconnection automatically
- Whenever `price_cache.version` changes (checked every ~500ms), pushes a single batched JSON object keyed by ticker symbol containing all actively tracked tickers (`watchlist ∪ positions`)
- Each ticker entry contains the 7 fields from `PriceUpdate.to_dict()`: `ticker`, `price`, `previous_price`, `timestamp`, `change`, `change_percent`, and `direction` (`"up"`, `"down"`, or `"flat"`), where `change` and `change_percent` are tick-to-tick relative to `previous_price`
- When the cache becomes empty, the stream must emit `data: {}` so the client clears its last ticker (*Task 0: today `stream.py` skips emitting when there are no prices*)
- `create_stream_router(price_cache)` in `backend/app/market/stream.py` must instantiate a fresh `APIRouter` inside the factory function, so multiple app instances in tests do not register duplicate routes (*Task 0: today it attaches to a module-level `router`*)

### Known Gaps (Task 0)

The market data subsystem is complete and tested, but it does **not** yet implement the following behaviours that the rest of this plan relies on. Fixing them is the **backend agent's first task (milestone M0, §13)**. Until then, agents must not assume these behaviours exist.

| # | Required behaviour | Current code | Fix |
|---|---|---|---|
| T0.1 | Sources normalize tickers | `simulator.py:242-255` (`SimulatorDataSource.add_ticker/remove_ticker`) does not normalize; `massive_client.py` `start()` (line 43) does not normalize | Apply `strip().upper()` in `start()`, `add_ticker()`, `remove_ticker()` of both sources |
| T0.2 | `PriceCache.remove()` bumps `version` | `cache.py:59-62` only pops the entry | Increment `_version` inside the lock |
| T0.3 | Empty cache emits `data: {}` | `stream.py` guards the yield with `if prices:` | Emit the (possibly empty) JSON object whenever the version changes |
| T0.4 | Fresh router per factory call | `stream.py:17` module-level `router` | Create the `APIRouter` inside `create_stream_router()` |

Each fix needs a unit test in `backend/tests/market/`. Update `planning/MARKET_DATA_SUMMARY.md` when they are done.

---

## 7. Database

### SQLite with Lifespan Initialization

The backend initializes the SQLite database during **FastAPI lifespan startup**, alongside starting the `MarketDataSource` (seeded with `watchlist ∪ positions` tickers) and the 30-second portfolio snapshot background task. If the database file doesn't exist or tables are missing, it creates the schema, seeds default data, and inserts an initial portfolio snapshot (no per-request lazy checks). This means:

- No separate migration step
- No manual database setup
- Fresh Docker volumes start with a clean, seeded database automatically

### Schema

All operational tables (`watchlist`, `positions`, `trades`, `portfolio_snapshots`, `chat_messages`) include a `user_id` column defaulting to `"default"`, while `users_profile` uses `id` (default `"default"`) as its primary key. This is hardcoded for now (single-user) but enables future multi-user support without schema migration.

**users_profile** — User state (cash balance)
- `id` TEXT PRIMARY KEY (default: `"default"`)
- `cash_balance` REAL (default: `10000.0`)
- `created_at` TEXT (ISO timestamp)

**watchlist** — Tickers the user is watching
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `added_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`

**positions** — Current holdings (one row per ticker per user)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `quantity` REAL (fractional shares supported)
- `avg_cost` REAL
- `updated_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`

**trades** — Trade history (append-only log)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `side` TEXT (`"buy"` or `"sell"`)
- `quantity` REAL (fractional shares supported)
- `price` REAL
- `executed_at` TEXT (ISO timestamp)

**portfolio_snapshots** — Portfolio value over time (for P&L chart). Recorded at initial seed/startup, every 30 seconds by a background task, and immediately after each trade execution.
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp)

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON — executed and failed actions; null for user messages)
- `created_at` TEXT (ISO timestamp)

### Default Seed Data

- One user profile: `id="default"`, `cash_balance=10000.0`
- Ten watchlist entries: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX
- One initial `portfolio_snapshots` entry with `total_value=10000.0` so the P&L chart has a starting data point immediately on first launch

### Position Accounting & Valuation Rules

- **Buy orders**:
  - Require normalized ticker (`^[A-Z]{1,5}$`) and finite `quantity > 0`.
  - If the ticker is not already in `watchlist`, automatically add it to `watchlist` and `MarketDataSource`.
  - Require a valid cached price in `PriceCache`. If `PriceCache.get_price(ticker)` is `None` (e.g., newly added in Massive mode before the first poll completes), reject with `400 Bad Request` (`"Price not yet available for <TICKER>"`).
  - Require `cash_balance >= quantity * price`. Deduct `quantity * price` from `cash_balance` and upsert `positions` using weighted-average cost: `new_avg_cost = (old_qty * old_avg_cost + quantity * price) / (old_qty + quantity)`.
- **Sell orders**:
  - Require normalized ticker (`^[A-Z]{1,5}$`), finite `quantity > 0`, a valid cached price in `PriceCache`, and an existing position with `held_quantity >= quantity - 1e-9`.
  - Add `quantity * price` to `cash_balance`. On partial sells, `avg_cost` remains unchanged.
  - If remaining quantity `<= 1e-9`, delete the position row from `positions`; if the ticker is also not in `watchlist`, call `market_data_source.remove_ticker(ticker)`.
- **Atomicity**: Cash balance updates, position updates/deletions, trade log insertion, and the post-trade `portfolio_snapshots` insertion must execute within a single SQLite transaction.
- **Missing-price fallback**: When computing portfolio value for `GET /api/portfolio` or `portfolio_snapshots`, if a held ticker has no price in `PriceCache` (`None`), fall back to its `avg_cost`.

---

## 8. API Endpoints

### Market Data
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/stream/prices` | SSE stream of live price updates |

### Portfolio
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/portfolio` | Current positions, cash balance, total value, unrealized P&L |
| POST | `/api/portfolio/trade` | Execute a trade: `{ticker, quantity, side}` |
| GET | `/api/portfolio/history` | Last 500 portfolio value snapshots ordered by `recorded_at ASC` (for P&L chart) |

### Watchlist
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Current watchlist tickers with latest prices |
| POST | `/api/watchlist` | Add a ticker: `{ticker}` |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/chat/history` | Load up to the last 50 persisted chat messages ordered by `created_at ASC` |
| POST | `/api/chat` | Send a message, receive complete JSON response (message + executed/failed actions) |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (for Docker/deployment) |

### Request/Response Schemas & Error Conventions

- **Standard error format**: `{"detail": "Human-readable error message"}`
  - `400 Bad Request`: Domain errors: insufficient cash/shares, or price not yet available in `PriceCache`
  - `401 Unauthorized`: Missing or wrong `APP_PASSWORD` when one is configured (§11 Security)
  - `404 Not Found`: Ticker not found in watchlist (on `DELETE /api/watchlist/{ticker}`)
  - `409 Conflict`: Ticker already present in watchlist (on `POST /api/watchlist`)
  - `422 Unprocessable Entity`: Request fails Pydantic validation, including a ticker that doesn't match `^[A-Z]{1,5}$` after normalization, or a `quantity` that isn't finite and `> 0`
  - `429 Too Many Requests`: `/api/chat` rate limit exceeded (§11 Security)
- **Contract source of truth**: FastAPI's generated `/openapi.json` is the authoritative API contract. The examples below are illustrative. The frontend generates its TypeScript API types from the OpenAPI schema (e.g. `openapi-typescript`) instead of hand-copying these shapes, so the two sides can't drift. All request/response bodies must therefore be declared as Pydantic models on the routes.
- **`GET /api/portfolio`**:
  ```json
  {
    "cash_balance": 8100.0,
    "total_value": 10050.0,
    "total_unrealized_pnl": 50.0,
    "total_unrealized_pnl_pct": 2.63,
    "positions": [
      {
        "ticker": "AAPL",
        "quantity": 10.0,
        "avg_cost": 190.0,
        "current_price": 195.0,
        "market_value": 1950.0,
        "unrealized_pnl": 50.0,
        "unrealized_pnl_pct": 2.63,
        "weight_pct": 19.4
      }
    ]
  }
  ```
- **`POST /api/portfolio/trade`**:
  - Request: `{"ticker": "AAPL", "side": "buy", "quantity": 10}`
  - Response (`200 OK`): Returns the executed `trade` record alongside the updated portfolio summary:
    ```json
    {
      "trade": {
        "id": "uuid",
        "ticker": "AAPL",
        "side": "buy",
        "quantity": 10.0,
        "price": 190.0,
        "executed_at": "2026-01-01T12:00:00Z"
      },
      "portfolio": { "...": "same shape as GET /api/portfolio" }
    }
    ```
- **`GET /api/portfolio/history`**:
  - Response (`200 OK`): Array of up to the most recent 500 snapshots ordered by `recorded_at ASC`:
    ```json
    [
      {"total_value": 10000.0, "recorded_at": "2026-01-01T12:00:00Z"}
    ]
    ```
- **Watchlist endpoints**:
  - `GET /api/watchlist`: Returns `{"tickers": [{"ticker": "AAPL", "price": 190.0, "previous_price": 189.9, "change": 0.1, "change_percent": 0.0527, "direction": "up", "timestamp": 1700000000.0, "added_at": "..."}]}` (`price` fields are `null` until the first poll/tick populates `PriceCache`).
  - `POST /api/watchlist`: Request `{"ticker": "PYPL"}` → normalizes to uppercase, inserts into `watchlist`, calls `market_data_source.add_ticker("PYPL")`, returns `201 Created` (or `409 Conflict` if already watched).
  - `DELETE /api/watchlist/{ticker}`: Normalizes `{ticker}`, removes it from `watchlist`, returns `200 OK` (`{"ticker": "AAPL", "removed": true}`) or `404 Not Found` if not watched. Removing a held ticker from `watchlist` succeeds and keeps the ticker active in `MarketDataSource` while the position remains open.
- **Chat endpoints**:
  - `GET /api/chat/history`: Returns `{"messages": [{"id": "uuid", "role": "user"|"assistant", "content": "...", "actions": [...] | null, "created_at": "..."}]}` (up to last 50 rows, `created_at ASC`).
  - `POST /api/chat`: Request `{"message": "Buy 10 AAPL"}` → returns `{"id": "uuid", "role": "assistant", "content": "...", "actions": [{"type": "trade"|"watchlist", "ticker": "AAPL", "side": "buy", "quantity": 10.0, "price": 190.0, "action": null, "status": "executed"|"failed", "error": null}], "created_at": "..."}`.

---

## 9. LLM Integration

When writing code to make calls to LLMs, use cerebras-inference skill to use LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider. Structured Outputs should be used to interpret the results.

> **Important: use the async API.** The skill's examples call synchronous `litellm.completion(...)`. Calling that from an `async def` FastAPI handler blocks the event loop, which freezes simulator ticks, SSE streams for every client, and trades for the whole duration of each chat request. The backend **must** call **`await litellm.acompletion(...)`** with the same model, `response_format`, `reasoning_effort` and `extra_body` arguments, wrapped in `asyncio.wait_for(..., timeout=30)`.

There is an OPENROUTER_API_KEY in the .env file in the project root.

### How It Works

When the user sends a chat message, the backend performs a **single LLM call** per message (no second LLM call if an action fails):

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads the most recent 20 messages from the `chat_messages` table as conversation context, and persists the incoming user message
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via `await litellm.acompletion(...)` → OpenRouter with a 30-second timeout, requesting structured output per the cerebras-inference skill (or uses mock mode if `LLM_MOCK=true`)
5. Parses the complete structured JSON response with `LLMResponse.model_validate_json(...)`. On timeout, provider error, or invalid JSON, it retries **once**. If that also fails, it returns **HTTP 200** with a fallback assistant message (e.g. "Sorry, I couldn't process that request. Please try again.") and `actions: []`. The UI keeps a single success code path, and the fallback is persisted like any other assistant message.
6. Auto-executes each requested trade and watchlist change in order, capturing per-action outcomes (`status: "executed" | "failed"` and `error: str | None`)
7. Stores the assistant message and the resulting `actions` array in `chat_messages`
8. Returns `{id, role: "assistant", content, actions, created_at}` to the frontend (no token-by-token streaming — Cerebras inference is fast enough that a loading indicator is sufficient), and the UI displays both executed and failed action badges inline

### Structured Output Schema

The LLM is instructed to respond with JSON matching this schema:

```json
{
  "message": "Your conversational response to the user",
  "trades": [
    {"ticker": "AAPL", "side": "buy", "quantity": 10}
  ],
  "watchlist_changes": [
    {"ticker": "PYPL", "action": "add"}
  ]
}
```

- `message` (required): The conversational text shown to the user
- `trades` (required, may be `[]`): Array of trades to auto-execute. Each trade goes through the same validation as manual trades (sufficient cash for buys, sufficient shares for sells)
- `watchlist_changes` (required, may be `[]`): Array of watchlist modifications

All fields are **required** (no defaults) so the Pydantic model works with strict structured outputs. The prompt tells the model to use `[]` when there are no actions. The model to pass as `response_format`:

```python
class Trade(BaseModel):
    ticker: str
    side: Literal["buy", "sell"]
    quantity: float

class WatchlistChange(BaseModel):
    ticker: str
    action: Literal["add", "remove"]

class LLMResponse(BaseModel):
    message: str
    trades: list[Trade]
    watchlist_changes: list[WatchlistChange]
```

Tickers and quantities returned by the LLM pass through the same API-layer validation as manual requests (§6, §8). An action that fails validation is recorded with `status: "failed"`.

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:
- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

If a trade or watchlist change fails validation (e.g., insufficient cash, insufficient shares, or price unavailable), that action is recorded in the response's `actions` array with `status: "failed"` and a descriptive `error` string so the UI can display the failure inline without making a second LLM call.

### System Prompt Guidance

The LLM should be prompted as "FinAlly, an AI trading assistant" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven in responses
- Always respond with valid structured JSON

### LLM Mock Mode

When `LLM_MOCK=true`, the backend returns deterministic mock responses instead of calling OpenRouter using case-insensitive pattern matching on the user's message:
- `"buy <qty> <ticker>"` → returns a confirmation message and `trades: [{"ticker": "<TICKER>", "side": "buy", "quantity": <qty>}]`
- `"sell <qty> <ticker>"` → returns a confirmation message and `trades: [{"ticker": "<TICKER>", "side": "sell", "quantity": <qty>}]`
- `"watch <ticker>"` or `"add <ticker>"` → returns a confirmation message and `watchlist_changes: [{"ticker": "<TICKER>", "action": "add"}]`
- `"unwatch <ticker>"` or `"remove <ticker>"` → returns a confirmation message and `watchlist_changes: [{"ticker": "<TICKER>", "action": "remove"}]`
- Any other message → returns a deterministic summary of the user's current cash balance, positions count, and total portfolio value with `trades: []` and `watchlist_changes: []`

This enables fast, free, reproducible E2E tests, development without an API key, and CI/CD pipelines.

---

## 10. Frontend Design

### Layout

The frontend is a single-page application with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), tick-to-tick change % (`change_percent` from the SSE `PriceUpdate` payload), and a sparkline mini-chart (accumulated from SSE since page load)
- **Main chart area** — larger chart for the currently selected ticker, with at minimum price over time. Clicking a ticker in the watchlist selects it here.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss)
- **P&L chart** — line chart showing total portfolio value over time, using data from `portfolio_snapshots` (`GET /api/portfolio/history`)
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change (unrealized return relative to average cost: `(current_price - avg_cost) / avg_cost * 100`)
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill.
- **AI chat panel** — docked/collapsible sidebar. Loads prior conversation history from `GET /api/chat/history` on mount, provides message input, scrolling conversation history, and a loading indicator while waiting for the LLM response. Trade executions and watchlist changes (`actions`) are shown inline as status badges (`executed` or `failed` with error reason).
- **Header** — portfolio total value (computed reactively on the client from SSE prices: `cash_balance + Σ(position.quantity × live_sse_price)`), connection status indicator, cash balance. The frontend re-fetches `/api/portfolio` and `/api/watchlist` after manual trades or chat responses.

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`
- Each SSE message contains the full dictionary of currently active prices, so the frontend replaces (or syncs keys with) its local price map on each event and derives live portfolio valuation/P&L client-side
- Canvas-based charting library preferred (Lightweight Charts or Recharts) for performance
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed
- API request/response TypeScript types are generated from the backend's `/openapi.json` (see §8 "Contract source of truth"), not hand-written
- Tailwind CSS for styling with a custom dark theme

---

## 11. Docker & Deployment

### Multi-Stage Dockerfile

```
Stage 1: Node 20 slim
  - Copy frontend/
  - npm install && npm run build (produces static export)

Stage 2: Python 3.12 slim
  - Install uv
  - WORKDIR /app
  - Copy backend/ into /app (code package lands at /app/app)
  - uv sync --frozen --no-dev (install Python dependencies from lockfile)
  - Copy frontend build output (frontend/out) into /app/static
  - Expose port 8000
  - CMD: uvicorn app.main:app --host 0.0.0.0 --port 8000   (single worker; no --workers)
```

**Image layout** (fixed; agents must not deviate):

| Path in container | Contents |
|---|---|
| `/app/app/` | Backend Python package (`market/`, `db/`, routes, …) |
| `/app/static/` | Next.js static export |
| `/app/db/` | **Volume mount**: SQLite file only (`/app/db/finally.db`) |

Because all code lives under `/app/app/`, the volume mounted at `/app/db` can never hide schema or seed code (see §4).

FastAPI serves the static frontend files and all API routes on port 8000. The app must run as a **single process with a single worker**: `PriceCache`, the market data task and the snapshot task live in memory, so multiple workers or instances would each run their own simulator with different prices.

### Docker Volume

In production and the provided start/stop scripts, the SQLite database persists via a named Docker volume mapped to `/app/db`:

```bash
docker run -v finally-data:/app/db -p 127.0.0.1:8000:8000 --env-file .env finally
```

The port is published on `127.0.0.1` only, so the app (which has no login) is not reachable from other machines on the network. See Security below.

For local container development where direct host-filesystem access to `db/finally.db` is desired, you can optionally bind-mount the project's `db/` directory instead (`-v $(pwd)/db:/app/db`). In both cases, `DB_PATH` defaults to `/app/db/finally.db` inside the container.

### Start/Stop Scripts

**`scripts/start_mac.sh`** (macOS/Linux):
- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container with the volume mount, localhost-only port mapping (`-p 127.0.0.1:8000:8000`), and `.env` file
- Prints the URL to access the app
- Optionally opens the browser

**`scripts/stop_mac.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT remove the volume (data persists)

**`scripts/start_windows.ps1`** / **`scripts/stop_windows.ps1`**: PowerShell equivalents for Windows.

All scripts should be idempotent — safe to run multiple times.

### Security

The app has no user accounts, and `/api/chat` spends the owner's OpenRouter credits, so exposure is controlled as follows:

- **Local default**: The port is bound to `127.0.0.1` only (start scripts and docs). No password is needed.
- **`APP_PASSWORD`**: When set, a small FastAPI middleware requires it on every `/api/*` request, sent as the `X-App-Password` header (and, because `EventSource` cannot set headers, as a `?key=` query parameter on `/api/stream/prices`). Missing or wrong values get `401`. The frontend prompts for the password once and keeps it in `sessionStorage`. `/api/health` is exempt.
- **Chat rate limit**: `/api/chat` is limited in memory to **10 requests per minute** (fixed window, single-user). Excess requests get `429`.

### Optional Cloud Deployment

The container is designed to deploy to AWS App Runner, Render, or any container platform. A Terraform configuration for App Runner may be provided in a `deploy/` directory as a stretch goal, but is not part of the core build.

Any network-reachable deployment **must** set either `APP_PASSWORD` or `LLM_MOCK=true`, and must run **exactly one instance** (e.g. App Runner max instances = 1), as explained above.

---

## 12. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**:
- Market data: simulator generates valid prices, GBM math is correct, Massive API response parsing works, both implementations conform to the abstract interface
- Portfolio: trade execution logic, P&L calculations, edge cases (selling more than owned, buying with insufficient cash, selling at a loss)
- LLM: structured output parsing handles all valid schemas, graceful handling of malformed responses, trade validation within chat flow
- API routes: correct status codes, response shapes, error handling

**Frontend (React Testing Library or similar)**:
- Component rendering with mock data
- Price flash animation triggers correctly on price changes
- Watchlist CRUD operations
- Portfolio display calculations
- Chat message rendering and loading state

### E2E Tests (in `test/`)

**Infrastructure**: A separate `docker-compose.test.yml` in `test/` that spins up the app container plus a Playwright container. This keeps browser dependencies out of the production image.

**Environment**: Tests run with `LLM_MOCK=true` by default for speed and determinism.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell shares: cash increases, position updates or disappears
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send a message, receive a response, trade execution appears inline
- SSE resilience: disconnect and verify reconnection

---

## 13. Build Plan

Agents build FinAlly in the milestones below, in order. A milestone is **done** only when its acceptance check passes. Later milestones may start once the contract they depend on is in place (e.g. the frontend can start M3 once M1's OpenAPI schema exists), but they may not merge before their dependencies are done.

| Milestone | Owner | Scope | Acceptance check |
|---|---|---|---|
| **M0: Market-data fixes** | Backend | Task 0 items T0.1–T0.4 (§6) | `uv run --extra dev pytest` green, including new tests for each fix; `MARKET_DATA_SUMMARY.md` updated |
| **M1: DB and REST API** | Backend | `backend/app/db/` (schema, seed, lifespan init), portfolio/watchlist/health routes, snapshot task, `APP_PASSWORD` middleware | pytest green; `curl` responses for every §8 endpoint match `/openapi.json` and the §8 examples |
| **M2: LLM chat** | Backend | Mock mode first, then real `acompletion` integration, fallback handling, rate limit | Mock-mode tests green; one real call succeeds with `OPENROUTER_API_KEY`; SSE keeps streaming during a chat request |
| **M3: Frontend** | Frontend | All §10 components; types generated from `/openapi.json` | `npm run build` produces a static export; frontend unit tests green; works end-to-end against the running backend |
| **M4: Docker and scripts** | DevOps | Dockerfile per §11 image layout, start/stop scripts, `.env.example`, `.gitignore` entries | `scripts/start_mac.sh` → app on `http://localhost:8000`, `/api/health` OK, data survives a restart |
| **M5: E2E tests** | QA | `test/docker-compose.test.yml` and the §12 Playwright scenarios | `docker compose -f test/docker-compose.test.yml up --exit-code-from playwright` passes |

**Coordination rules**
- `planning/PLAN.md` and the backend's `/openapi.json` together form the contract. Any change to an API shape is made in the backend's Pydantic models first, then the frontend types are regenerated.
- An agent that finds the plan and the code in conflict records it in `planning/` (and fixes it, if it is within the agent's milestone) rather than silently working around it.
