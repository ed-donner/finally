# FinAlly — AI Trading Workstation

## Project Specification

## 1. Vision

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

## 2. User Experience

### First Launch

The user runs a single Docker command (or a provided start script) and opens `http://localhost:8000` in a browser. No login, no signup. They immediately see:

- A watchlist of 10 default tickers with live-updating prices in a grid
- $10,000 in virtual cash
- A dark, data-rich trading terminal aesthetic
- An AI chat panel ready to assist

### What the User Can Do

- **Watch prices stream** — prices flash green (uptick) or red (downtick) with subtle CSS animations that fade
- **View sparkline mini-charts** — price action beside each ticker in the watchlist, accumulated on the frontend from the SSE stream since page load (sparklines fill in progressively)
- **Click a ticker** to see a larger detailed chart in the main chart area
- **Buy and sell shares** — market orders only, whole shares only, instant fill at current price, no fees, no confirmation dialog
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
│   └── db/                   # Schema definitions, seed data, migration logic
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start_mac.sh          # Launch Docker container (macOS/Linux)
│   ├── stop_mac.sh           # Stop Docker container (macOS/Linux)
│   ├── start_windows.ps1     # Launch Docker container (Windows PowerShell)
│   └── stop_windows.ps1      # Stop Docker container (Windows PowerShell)
├── test/                     # Playwright E2E tests + docker-compose.test.yml
├── db/                       # SQLite file location for local (non-Docker) runs
│   └── .gitkeep              # Directory exists in repo; finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/db/`** contains schema SQL definitions and seed logic. The backend lazily initializes the database on first request — creating tables and seeding default data if the SQLite file doesn't exist or is empty.
- **`db/`** at the top level is where the SQLite file (`db/finally.db`) lives when the backend runs outside Docker. In Docker, the database lives in the `finally-data` named volume mounted at `/app/db` and persists across container restarts; the repo's `db/` directory is not mounted.
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
```

### Behavior

- If `MASSIVE_API_KEY` is set and non-empty → backend uses Massive REST API for market data
- If `MASSIVE_API_KEY` is absent or empty → backend uses the built-in market simulator
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses (for E2E tests)
- The backend reads `.env` from the project root (mounted into the container or read via docker `--env-file`)

---

## 6. Market Data

### Two Implementations, One Interface

Both the simulator and the Massive client implement the same abstract interface. The backend selects which to use based on the environment variable. All downstream code (SSE streaming, price cache, frontend) is agnostic to the source.

### Simulator (Default)

- Generates prices using geometric Brownian motion (GBM) with configurable drift and volatility per ticker
- Updates at ~500ms intervals
- Correlated moves across tickers (e.g., tech stocks move together)
- Occasional random "events" — sudden 2-5% moves on a ticker for drama
- Starts from realistic seed prices (e.g., AAPL ~$190, GOOGL ~$175, etc.)
- Runs as an in-process background task — no external dependencies

### Massive API (Optional)

- REST API polling (not WebSocket) — simpler, works on all tiers
- Polls for the union of all watched tickers on a configurable interval
- Free tier (5 calls/min): poll every 15 seconds
- Paid tiers: poll every 2-15 seconds depending on tier
- Parses REST response into the same format as the simulator

### Shared Price Cache

- A single background task (simulator or Massive poller) writes to an in-memory price cache
- The cache holds the latest price, previous price, and timestamp for each ticker
- SSE streams read from this cache and push updates to connected clients
- This architecture supports future multi-user scenarios without changes to the data layer

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- Server pushes price updates for all tickers known to the system at a regular cadence (~500ms) — in the single-user model this is equivalent to the user's watchlist
- Each SSE event contains ticker, price, previous price, timestamp, and change direction
- Client handles reconnection automatically (EventSource has built-in retry)

---

## 7. Database

### SQLite with Lazy Initialization

The backend checks for the SQLite database on startup (or first request). If the file doesn't exist or tables are missing, it creates the schema and seeds default data. This means:

- No separate migration step
- No manual database setup
- Fresh Docker volumes start with a clean, seeded database automatically

### Schema

All tables include a `user_id` column defaulting to `"default"`. This is hardcoded for now (single-user) but enables future multi-user support without schema migration. Do not build anything around it: a single module-level constant supplies the value, with no user-resolution middleware, request parameters, or per-user logic.

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
- `quantity` INTEGER (whole shares only)
- `avg_cost` REAL
- `updated_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`

**trades** — Trade history (append-only log)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `side` TEXT (`"buy"` or `"sell"`)
- `quantity` INTEGER (whole shares only)
- `price` REAL
- `executed_at` TEXT (ISO timestamp)

**portfolio_snapshots** — Portfolio value over time (for P&L chart). Recorded once at backend startup and immediately after each trade execution. There is no periodic snapshot task; the frontend extends the P&L line to the present using live prices.
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp)

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON — trades executed, watchlist changes made; null for user messages)
- `created_at` TEXT (ISO timestamp)

### Default Seed Data

- One user profile: `id="default"`, `cash_balance=10000.0`
- Ten watchlist entries: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX

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
| POST | `/api/portfolio/trade` | Execute a trade: `{ticker, quantity, side}` — `quantity` is a positive integer |
| GET | `/api/portfolio/history` | Portfolio value snapshots over time (for P&L chart) |

### Watchlist
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Current watchlist tickers with latest prices |
| POST | `/api/watchlist` | Add a ticker: `{ticker}` |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker. Rejected with 409 while a position is open in that ticker, so every held ticker always has a live price |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat` | Send a message, receive complete JSON response (message + executed actions) |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (for Docker/deployment) |

### API Contracts

These shapes are the contract between the frontend and backend agents. Field names are exact.

#### Conventions

- All request and response bodies are JSON.
- Tickers are normalized by the backend (trimmed, uppercased) and must match `^[A-Z][A-Z0-9.]{0,9}$`. Responses always carry the normalized form.
- REST timestamps are ISO 8601 UTC strings. The SSE stream is the exception: its `timestamp` is Unix seconds (float), as already implemented.
- Monetary values are plain JSON numbers; the frontend handles display formatting.
- `side` is `"buy"` or `"sell"`. `quantity` is a positive integer.

#### Errors

Every non-2xx response, including request validation failures, uses one body shape (a FastAPI exception handler replaces the default `{"detail": ...}`):

```json
{"error": {"code": "insufficient_cash", "message": "Need $1,912.40 but only $850.00 is available."}}
```

`message` is written for the end user and can be displayed as is. `code` is one of:

| Code | Status | When |
|---|---|---|
| `invalid_request` | 422 | Malformed body, missing field, wrong type, quantity not a positive integer, unknown `side` |
| `invalid_ticker` | 400 | Ticker fails the format rule above |
| `ticker_not_watched` | 400 | Trade requested for a ticker that is not on the watchlist |
| `price_unavailable` | 409 | Ticker is on the watchlist but has no price in the cache yet |
| `insufficient_cash` | 400 | Buy costs more than the cash balance |
| `insufficient_shares` | 400 | Sell quantity exceeds the held quantity (including no position) |
| `not_watched` | 404 | Removing a ticker that is not on the watchlist |
| `position_open` | 409 | Removing a ticker that has an open position |
| `llm_error` | 502 | LLM call failed, timed out, or returned output that does not match the schema |

#### `GET /api/stream/prices`

Already implemented. The stream opens with `retry: 1000`, then sends one unnamed event roughly every 500ms whenever any price has changed. Each event carries every tracked ticker, keyed by ticker:

```
data: {"AAPL": {"ticker": "AAPL", "price": 191.24, "previous_price": 191.10, "timestamp": 1791100800.12, "change": 0.14, "change_percent": 0.0733, "direction": "up"}, "MSFT": {...}}
```

`change`, `change_percent` and `direction` (`"up"`, `"down"`, `"flat"`) compare against the previous tick, not the start of the day.

#### `GET /api/portfolio`

```json
{
  "cash_balance": 8087.60,
  "positions_value": 1962.40,
  "total_value": 10050.00,
  "unrealized_pnl": 50.00,
  "positions": [
    {
      "ticker": "AAPL",
      "quantity": 10,
      "avg_cost": 191.24,
      "current_price": 196.24,
      "market_value": 1962.40,
      "unrealized_pnl": 50.00,
      "unrealized_pnl_percent": 2.61
    }
  ]
}
```

`total_value` = `cash_balance` + `positions_value`. `positions` is an empty array when nothing is held, ordered by ticker.

#### `POST /api/portfolio/trade`

Request:

```json
{"ticker": "AAPL", "quantity": 10, "side": "buy"}
```

Response `200` — the executed trade plus the full updated portfolio (same shape as `GET /api/portfolio`), so the frontend does not need a second request:

```json
{
  "trade": {
    "id": "0b6f3c1e-…",
    "ticker": "AAPL",
    "side": "buy",
    "quantity": 10,
    "price": 191.24,
    "executed_at": "2026-10-04T14:03:22Z"
  },
  "portfolio": { "cash_balance": 8087.60, "positions_value": 1912.40, "total_value": 10000.00, "unrealized_pnl": 0.0, "positions": [ … ] }
}
```

Trade rules, checked in this order:

1. The ticker must be on the watchlist, otherwise `ticker_not_watched`. Trades never add to the watchlist implicitly.
2. The ticker must have a price in the cache, otherwise `price_unavailable`. The fill price is the cached price at the moment of execution. In simulator mode a newly added ticker is priced immediately; in Massive mode it is priced on the next poll (up to 15 seconds on the free tier).
3. Buys need `quantity × price ≤ cash_balance`; sells need `quantity ≤` held quantity.

Errors: `invalid_request`, `invalid_ticker`, `ticker_not_watched`, `price_unavailable`, `insufficient_cash`, `insufficient_shares`.

#### `GET /api/portfolio/history`

```json
{
  "snapshots": [
    {"total_value": 10000.00, "recorded_at": "2026-10-04T14:00:00Z"},
    {"total_value": 10000.00, "recorded_at": "2026-10-04T14:03:22Z"}
  ]
}
```

Ordered oldest first. Returns all snapshots.

#### `GET /api/watchlist`

```json
{
  "watchlist": [
    {
      "ticker": "AAPL",
      "price": 191.24,
      "previous_price": 191.10,
      "direction": "up",
      "day_change_percent": 0.65,
      "added_at": "2026-10-04T14:00:00Z"
    }
  ]
}
```

Ordered by `added_at`. `price`, `previous_price`, `direction` and `day_change_percent` are `null` while the ticker has no price in the cache. The baseline for `day_change_percent` is still to be defined (Section 13, item 12).

#### `POST /api/watchlist`

Request: `{"ticker": "PYPL"}`

Response — a single watchlist entry in the same shape as an element of `GET /api/watchlist`. `201` when the ticker was added; `200` when it was already on the watchlist (the call is idempotent). The backend also registers the ticker with the market data source.

Errors: `invalid_request`, `invalid_ticker`.

#### `DELETE /api/watchlist/{ticker}`

Response `204` with no body. The backend also removes the ticker from the market data source.

Errors: `invalid_ticker`, `not_watched`, `position_open`.

#### `POST /api/chat`

Request: `{"message": "Buy 5 shares of PYPL"}`

Response `200` — the stored assistant message, with the outcome of every action it requested:

```json
{
  "message": {
    "id": "7d2a9f40-…",
    "role": "assistant",
    "content": "Added PYPL to your watchlist and bought 5 shares.",
    "created_at": "2026-10-04T14:05:10Z",
    "actions": {
      "watchlist_changes": [
        {"ticker": "PYPL", "action": "add", "status": "executed", "error": null}
      ],
      "trades": [
        {"ticker": "PYPL", "side": "buy", "quantity": 5, "status": "executed", "price": 71.30, "error": null},
        {"ticker": "TSLA", "side": "buy", "quantity": 500, "status": "failed", "price": null, "error": {"code": "insufficient_cash", "message": "Need $125,000.00 but only $9,643.50 is available."}}
      ]
    }
  }
}
```

- `status` is `"executed"` or `"failed"`. A failed action carries the same `error` object as the REST endpoints. A failed action does not fail the request: the response is still `200`.
- Both arrays are always present and may be empty.
- The same `actions` object is what gets stored in `chat_messages.actions`.
- After a response containing any action, the frontend refetches `/api/portfolio` and `/api/watchlist`.

Errors: `invalid_request` (empty or missing `message`), `llm_error`. On `llm_error` the user's message is still stored; no assistant message is stored.

#### `GET /api/health`

Response `200`: `{"status": "ok"}`

---

## 9. LLM Integration

When writing code to make calls to LLMs, use the `cerebras` skill to use LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider. Structured Outputs should be used to interpret the results.

There is an OPENROUTER_API_KEY in the .env file in the project root.

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads recent conversation history from the `chat_messages` table
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via LiteLLM → OpenRouter, requesting structured output, using the `cerebras` skill
5. Parses the complete structured JSON response
6. Auto-executes any trades or watchlist changes specified in the response
7. Stores the message and executed actions in `chat_messages`
8. Returns the complete JSON response to the frontend (no token-by-token streaming — Cerebras inference is fast enough that a loading indicator is sufficient)

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
- `trades` (optional): Array of trades to auto-execute. Quantities are whole shares. Each trade goes through the same validation as manual trades (sufficient cash for buys, sufficient shares for sells)
- `watchlist_changes` (optional): Array of watchlist modifications. Removals follow the same rule as the API: a ticker with an open position cannot be removed

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:
- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

Actions run through the same code paths and rules as the REST endpoints (Section 8), in a fixed order so a single response can add a ticker and buy it, or sell a position and remove the ticker:

1. Watchlist additions
2. Trades, in the order given
3. Watchlist removals

A trade is only possible for a ticker on the watchlist with a cached price, so the LLM must include a watchlist addition when it trades a new ticker. The system prompt states this rule.

If an action fails validation (e.g., insufficient cash, `ticker_not_watched`, `price_unavailable`), the remaining actions still run, and the failure is returned in the `actions` payload of the chat response. The frontend renders it inline next to the assistant message. There is no second LLM call; the failure is visible to the LLM on the next turn through the stored conversation history.

If the LLM call itself fails (missing key, timeout, or output that does not match the schema), `/api/chat` returns `llm_error` and no actions are executed.

### System Prompt Guidance

The LLM should be prompted as "FinAlly, an AI trading assistant" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven in responses
- Always respond with valid structured JSON

### LLM Mock Mode

When `LLM_MOCK=true`, the backend returns deterministic mock responses instead of calling OpenRouter. This enables:
- Fast, free, reproducible E2E tests
- Development without an API key
- CI/CD pipelines

---

## 10. Frontend Design

### Layout

The frontend is a single-page application with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), daily change %, and a sparkline mini-chart (accumulated from SSE since page load)
- **Main chart area** — larger chart for the currently selected ticker, with at minimum price over time. Clicking a ticker in the watchlist selects it here.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss)
- **P&L chart** — line chart showing total portfolio value over time, using data from `portfolio_snapshots` plus a live final point computed from current prices
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade bar** — simple input area: ticker field, quantity field (whole shares), buy button, sell button. Market orders, instant fill.
- **AI chat panel** — docked/collapsible sidebar. Message input, scrolling conversation history, loading indicator while waiting for LLM response. Trade executions and watchlist changes shown inline as confirmations.
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`
- One charting library only: Lightweight Charts (canvas) for the main chart and the P&L chart
- Sparklines are hand-rolled SVG polylines; the heatmap is a small custom treemap built with plain CSS/flex or SVG — no second charting library
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed
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
  - Copy backend/
  - uv sync (install Python dependencies from lockfile)
  - Copy frontend build output into a static/ directory
  - Expose port 8000
  - CMD: uvicorn serving FastAPI app
```

FastAPI serves the static frontend files and all API routes on port 8000.

### Docker Volume

The SQLite database persists via a named Docker volume:

```bash
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally
```

`finally-data` is a Docker-managed named volume mounted at `/app/db`; the backend writes `finally.db` there. The `db/` directory in the project root is not mounted into the container — it is only used when running the backend outside Docker.

### Start/Stop Scripts

**`scripts/start_mac.sh`** (macOS/Linux):
- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container with the named volume, port mapping, and `.env` file
- Prints the URL to access the app
- Stays thin: `--build` is the only flag

**`scripts/stop_mac.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT remove the volume (data persists)

**`scripts/start_windows.ps1`** / **`scripts/stop_windows.ps1`**: PowerShell equivalents for Windows.

All scripts should be idempotent — safe to run multiple times. The scripts are the only supported way to launch the app; there is no root `docker-compose.yml`.

Cloud deployment (Terraform, App Runner, etc.) is out of scope for this build.

---

## 12. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**:
- Market data: simulator generates valid prices, GBM math is correct, Massive API response parsing works, both implementations conform to the abstract interface
- Portfolio: trade execution logic, P&L calculations, edge cases (selling more than owned, buying with insufficient cash, selling at a loss, non-integer or non-positive quantity)
- Watchlist: removing a ticker with an open position is rejected
- LLM: structured output parsing handles all valid schemas, graceful handling of malformed responses, trade validation within chat flow
- API routes: correct status codes, response shapes, error handling

**Frontend (React Testing Library or similar)**:
- Component rendering with mock data
- Price flash animation triggers correctly on price changes
- Watchlist CRUD operations
- Portfolio display calculations
- Chat message rendering and loading state
- Connection status indicator reflects the `EventSource` state (connected / reconnecting / disconnected)

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

---

## 13. Review Notes — Open Questions & Decisions

Review of this document dated 2026-10-04, checked against the completed market data code (`backend/app/market/`) and the `cerebras` skill. Section 13.1 records decisions already applied to the sections above. Section 13.2 lists what is still open; item numbers are kept from the original review, so they are not contiguous. All items originally marked as blockers have been resolved.

### 13.1 Decisions Applied

| Decision | Where applied | Review items closed |
|---|---|---|
| LLM calls go through OpenRouter using `openrouter/openai/gpt-oss-120b`, with Cerebras as the inference provider and `OPENROUTER_API_KEY` as the key. Checked against the OpenRouter API on 2026-10-04: the model is listed, and Cerebras serves it with structured outputs and reasoning support. | Sections 5, 9; `cerebras` skill | 1, 2 |
| The skill is named `cerebras` (matching its directory). | Section 9; skill frontmatter | 3 |
| The database lives in the `finally-data` named volume in Docker. The repo's `db/` directory is for local non-Docker runs only. | Sections 4, 11 | 4 |
| No root `docker-compose.yml`; start/stop scripts are the only launch path. `test/docker-compose.test.yml` stays. | Sections 4, 11 | 5 |
| Snapshots are written at startup and after each trade only. No 30-second background task; the frontend adds a live final point to the P&L chart. | Sections 7, 10 | 20 |
| Cloud deployment (Terraform / App Runner) is out of scope. | Section 11 | — |
| One charting library: Lightweight Charts for the main and P&L charts, SVG polylines for sparklines, a custom CSS/SVG treemap for the heatmap. | Section 10 | 7 |
| The SSE reconnection E2E scenario is removed and replaced by a frontend unit test of the connection status indicator. | Section 12 | — |
| A ticker with an open position cannot be removed from the watchlist (409), so every held ticker always has a live price. | Sections 8, 9, 12 | 9 |
| `user_id` stays in the schema but is supplied by a single constant; nothing is built around it. | Section 7 | — |
| Whole-share trading only; `quantity` is a positive integer. | Sections 2, 7, 8, 9, 10, 12 | 16 |
| Start scripts stay thin (`--build` is the only flag) and do not open a browser. | Sections 2, 11 | 33 |
| Trades are only allowed for tickers on the watchlist that have a cached price; otherwise `ticker_not_watched` or `price_unavailable`. Trades never add to the watchlist implicitly. | Sections 8, 9 | 10 |
| API contracts are specified: request/response shapes, a single error body format, and error codes with status codes. | Section 8 | 15 |
| Chat actions run in a fixed order (watchlist additions, trades, watchlist removals). Failures are returned per action in the `actions` payload and rendered inline; there is no second LLM call. | Sections 8, 9 | 24, 28 |
| An LLM failure returns `llm_error` (502); the user message is stored, no actions run. | Sections 8, 9 | 25 |

### 13.2 Open Questions & Missing Specification

**Setup**

6. **`.env.example` is described as committed but does not exist** in the repo. It needs to be created, and the start scripts need defined behavior when `.env` is missing (`docker run --env-file .env` fails outright).
8. **Database init timing.** Section 4 says "lazily initializes on first request"; Section 7 says "on startup (or first request)". The startup snapshot and the market data source both need the database at startup, so init must happen in the FastAPI lifespan. Say so explicitly.

**Market data and watchlist**

11. **Ticker validation on add.** Format and normalization are now defined in Section 8. What remains: the simulator accepts any well-formed symbol and gives unknown ones (e.g. `ZZZZ`) a random seed price between $50 and $300 — is that acceptable? In Massive mode a non-existent symbol is accepted but never receives a price, so it sits on the watchlist with `null` prices and every trade returns `price_unavailable`; decide whether to detect and reject it.
12. **"Daily change %" is undefined.** `PriceUpdate` only carries the change against the previous tick. The simulator has no previous close. Define the baseline: seed price / first price seen since server start is the simplest option. For Massive, the previous close is available from the API.
13. **Main chart history.** Price history exists only in the browser since page load, so a refresh empties the main chart and all sparklines. Is that acceptable? If not, the backend needs a small rolling in-memory buffer per ticker and an endpoint to fetch it.
14. **Massive mode outside market hours.** Prices will be static on evenings and weekends, so there is no flashing and sparklines stay flat. Worth a note so it is not reported as a bug.

**Portfolio and trading**

17. **Position lifecycle.** Confirm: a full sell deletes the `positions` row; a buy into an existing position recalculates `avg_cost` as a weighted average; a partial sell leaves `avg_cost` unchanged.
18. **Realized P&L.** Only unrealized P&L is mentioned. Should realized P&L appear anywhere? Total portfolio value against the $10,000 start already captures it, which may be enough.
19. **Atomicity.** A trade touches cash, positions, trades and snapshots. It should be a single SQLite transaction, and manual trades must not interleave with LLM-initiated ones.
21. **Reset.** There is no way to return to $10,000 other than deleting the Docker volume. Useful for a demo; could be a documented command (`docker volume rm finally-data`) rather than an endpoint.

**Chat**

22. **No endpoint to load chat history.** Messages are stored in `chat_messages`, but only `POST /api/chat` exists, so the conversation disappears from the UI on page reload while the LLM still receives it. Recommend adding `GET /api/chat`.
23. **History window.** "Recent conversation history" — how many messages? Recommend a fixed number (e.g. the last 20).
26. **Mock responses.** `LLM_MOCK=true` returns "deterministic mock responses", and the E2E scenario expects an inline trade execution. Define the mock exactly (e.g. a fixed reply that buys 1 share of AAPL, or simple keyword matching) so the test and the backend agree.
27. **Schema nuance.** `trades` and `watchlist_changes` are described as optional. Strict Structured Outputs generally requires all fields to be present, so define them as required arrays that may be empty.

**Frontend and deployment**

29. **Refresh strategy.** After a trade (manual or via chat), how do the header, positions table, heatmap and P&L chart refresh — refetch `/api/portfolio` on each action, on a timer, or both? Live total value presumably comes from client-side recalculation using SSE prices; state that.
30. **Connection status.** Map explicitly to `EventSource.readyState` (OPEN = green, CONNECTING = yellow, CLOSED = red).
31. **Static serving.** Specify that API routers are registered before the static mount and that `/` serves `index.html`. As a single page with no client-side routes, no fallback routing is needed.
32. **Database path.** The path differs between Docker (`/app/db/finally.db`) and local development (`db/finally.db`). Recommend a `DB_PATH` environment variable with a sensible default.

### 13.3 Suggested Order of Resolution

No blockers remain. Suggested order for what is left:

1. Items 11–12 (ticker validation, daily change baseline) — affects the watchlist backend and the `day_change_percent` field.
2. Items 22, 23, 26, 27 (chat history endpoint, history window, mock responses, schema) — affects the chat UI and E2E tests.
3. Everything else can be decided by the owning agent, provided the decision is written back into this document.
