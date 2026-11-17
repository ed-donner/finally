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
- **Empty states**: with no positions, the heatmap and positions table show a short hint (e.g. "No positions yet — buy something or ask FinAlly") instead of blank space

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
│  └── /*              Static file serving        │
│                      (Next.js export)           │
│                                                 │
│  SQLite database (bind-mounted ./db)            │
│  Background tasks: market data sim/poller,      │
│                    portfolio snapshots          │
└─────────────────────────────────────────────────┘
```

- **Frontend**: Next.js with TypeScript, built as a static export (`output: 'export'`), served by FastAPI as static files
- **Backend**: FastAPI (Python), managed as a `uv` project
- **Database**: SQLite, single file at `db/finally.db`, bind-mounted from the project's `db/` directory for persistence
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
│       ├── market/           # Market data subsystem (complete)
│       └── db/               # Schema SQL, seed data, DB init logic
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start.sh              # Launch Docker container (macOS/Linux)
│   ├── stop.sh               # Stop Docker container (macOS/Linux)
│   ├── start.ps1             # Launch Docker container (Windows PowerShell)
│   └── stop.ps1              # Stop Docker container (Windows PowerShell)
├── test/                     # Playwright E2E tests + docker-compose.test.yml
├── db/                       # Runtime data dir, bind-mounted to /app/db (finally.db lives here)
│   └── .gitkeep              # Directory exists in repo; finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/app/db/`** contains schema SQL definitions and seed logic, inside the Python package alongside `app/market/`. The backend initializes the database at startup (see §7).
- **`db/`** at the top level is the runtime data directory. The SQLite file (`db/finally.db`) is created here by the backend — both when running locally with `uv run` and inside Docker, where `./db` is bind-mounted to `/app/db`. Deleting `db/finally.db` resets the app.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests and supporting infrastructure (`docker-compose.test.yml`). Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- **`scripts/`** contains start/stop scripts that wrap Docker commands. These are the only supported way to launch the container (no top-level `docker-compose.yml`).

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

# Optional: SQLite file location (default: <project root>/db/finally.db; /app/db/finally.db in Docker)
DB_PATH=
```

### Behavior

- If `MASSIVE_API_KEY` is set and non-empty → backend uses Massive REST API for market data
- If `MASSIVE_API_KEY` is absent or empty → backend uses the built-in market simulator
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses (for E2E tests)
- If `OPENROUTER_API_KEY` is missing and `LLM_MOCK` is not true → the app still runs; `/api/chat` replies with an assistant message explaining that the AI is not configured
- The backend reads `.env` from the project root (mounted into the container or read via docker `--env-file`)

---

## 6. Market Data

> **Status: complete.** See `planning/MARKET_DATA_SUMMARY.md`. The only remaining change is the small `session_open` extension described under *Session Change* below.

### Two Implementations, One Interface

Both the simulator and the Massive client implement the same abstract interface. The backend selects which to use based on the environment variable. All downstream code (SSE streaming, price cache, frontend) is agnostic to the source.

### Simulator (Default)

- Generates prices using geometric Brownian motion (GBM) with configurable drift and volatility per ticker
- Updates at ~500ms intervals
- Correlated moves across tickers (e.g., tech stocks move together)
- Occasional random "events" — sudden 2-5% moves on a ticker for drama
- Starts from realistic seed prices (e.g., AAPL ~$190, GOOGL ~$175, etc.); tickers without a seed price start at a random price between $50 and $300 with default GBM parameters
- Runs as an in-process background task — no external dependencies

### Massive API (Optional)

- REST API polling (not WebSocket) — simpler, works on all tiers
- Polls for the union of all tracked tickers in a single snapshot call per interval
- Free tier (5 calls/min): poll every 15 seconds
- Paid tiers: poll every 2-15 seconds depending on tier
- Parses REST response into the same format as the simulator

### Tracked Tickers

The set of tickers the market data source tracks is **watchlist ∪ tickers with open positions**. At startup the backend loads both from the database and calls `source.start(...)` with the union. Removing a ticker from the watchlist only calls `source.remove_ticker(...)` if there is no open position in it; closing a position in a ticker that isn't on the watchlist removes it likewise.

### Shared Price Cache

- A single background task (simulator or Massive poller) writes to an in-memory price cache
- The cache holds the latest price, previous price, timestamp, and session open price for each ticker
- SSE streams read from this cache and push updates to connected clients
- This architecture supports future multi-user scenarios without changes to the data layer

### Session Change

There is no real "previous close" in the simulator, so the watchlist's change column is **change since session open**, where *session open* is the first price the cache recorded for that ticker since the server started (for the simulator, the seed price). The same rule applies in Massive mode, keeping both sources identical. `PriceCache` records `session_open` on the first write for a ticker, and `PriceUpdate.to_dict()` adds `session_open` and `session_change_percent`. The UI labels the column "Chg %".

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- The server sends `retry: 1000` first, then checks the cache every ~500ms and emits an event only when prices have changed
- **Each event is one batch containing every tracked ticker**, keyed by symbol:

```
data: {"AAPL": {"ticker": "AAPL", "price": 190.52, "previous_price": 190.41,
                "timestamp": 1731753600.12, "change": 0.11, "change_percent": 0.0578,
                "direction": "up", "session_open": 190.0, "session_change_percent": 0.2737},
       "GOOGL": {...}, ...}
```

- `timestamp` is Unix seconds (float); `change`/`change_percent` are relative to the previous tick; `direction` is `"up" | "down" | "flat"`
- Client handles reconnection automatically (EventSource has built-in retry)

---

## 7. Database

### Initialization at Startup

On startup (FastAPI lifespan handler), the backend opens the SQLite file at `DB_PATH`, creates any missing tables (`CREATE TABLE IF NOT EXISTS`), and seeds default data if the `users_profile` table is empty. This means:

- No separate migration step
- No manual database setup, no per-request checks
- Fresh volumes start with a clean, seeded database automatically
- The watchlist and positions are available before the market data source starts

### Schema

Every table except `users_profile` includes a `user_id` column defaulting to `"default"`; `users_profile.id` is the user key itself. This is hardcoded for now (single-user) but enables future multi-user support without schema migration.

**users_profile** — User state (cash balance)
- `id` TEXT PRIMARY KEY (default: `"default"`) — the user id
- `cash_balance` REAL (default: `10000.0`)
- `created_at` TEXT (ISO timestamp)

**watchlist** — Tickers the user is watching
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `added_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`

**positions** — Current holdings (one row per ticker per user; row deleted when quantity reaches zero)
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

**portfolio_snapshots** — Portfolio value over time (for P&L chart). Recorded once at startup, every 30 seconds by a background task, and immediately after each trade execution.
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp)

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON — the executed-action results, see §9; null for user messages)
- `created_at` TEXT (ISO timestamp)

All timestamps are ISO 8601 UTC strings (e.g. `2026-11-16T14:03:22Z`).

### Default Seed Data

- One user profile: `id="default"`, `cash_balance=10000.0`
- Ten watchlist entries: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX

---

## 8. API Endpoints

### Summary

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/stream/prices` | SSE stream of live price updates (§6) |
| GET | `/api/portfolio` | Current positions, cash balance, total value, unrealized P&L |
| POST | `/api/portfolio/trade` | Execute a trade: `{ticker, quantity, side}` |
| GET | `/api/portfolio/history` | Portfolio value snapshots over time (for P&L chart) |
| GET | `/api/watchlist` | Current watchlist tickers with latest prices |
| POST | `/api/watchlist` | Add a ticker: `{ticker}` |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker |
| POST | `/api/chat` | Send a message, receive complete JSON response (message + executed actions) |
| GET | `/api/chat/history` | Previous chat messages (so the chat panel survives a page refresh) |
| GET | `/api/health` | Health check: `{"status": "ok"}` |

### Conventions

- All request/response bodies are JSON. Numbers are plain floats; the frontend formats them (2 decimals for money, up to 4 for quantity).
- Tickers are normalized to uppercase by the backend and must match `^[A-Z][A-Z.]{0,5}$`.
- Errors use FastAPI's standard shape with a human-readable message: `{"detail": "Insufficient cash: need $1,905.20, have $1,200.00"}`
  - `400` — business-rule failures (insufficient cash/shares, no price available, watchlist full)
  - `404` — removing a ticker not on the watchlist
  - `409` — adding a ticker already on the watchlist
  - `422` — malformed request (FastAPI/Pydantic validation)

### Response Shapes

**`GET /api/portfolio`**
```json
{
  "cash_balance": 8094.80,
  "positions_value": 1960.15,
  "total_value": 10054.95,
  "unrealized_pnl": 54.95,
  "positions": [
    {"ticker": "AAPL", "quantity": 10, "avg_cost": 190.52, "current_price": 196.02,
     "market_value": 1960.20, "unrealized_pnl": 55.00, "unrealized_pnl_percent": 2.887}
  ]
}
```
If a position's price is not yet in the cache, `current_price` falls back to `avg_cost`.

**`POST /api/portfolio/trade`** — request `{"ticker": "AAPL", "quantity": 10, "side": "buy"}`; response:
```json
{
  "trade": {"id": "…", "ticker": "AAPL", "side": "buy", "quantity": 10, "price": 190.52,
            "executed_at": "2026-11-16T14:03:22Z"},
  "portfolio": { "...same shape as GET /api/portfolio..." }
}
```

**`GET /api/portfolio/history`** — most recent 1,000 snapshots, oldest first:
```json
[{"recorded_at": "2026-11-16T14:03:00Z", "total_value": 10000.0}, ...]
```

**`GET /api/watchlist`** — in the order added:
```json
[{"ticker": "AAPL", "price": 190.52, "previous_price": 190.41, "session_change_percent": 0.27,
  "added_at": "2026-11-16T14:00:00Z"}, ...]
```
`price` and related fields are `null` if the ticker has no cached price yet.

**`POST /api/watchlist`** — request `{"ticker": "PYPL"}`; returns `201` with the new watchlist entry (same shape as an item above).

**`DELETE /api/watchlist/{ticker}`** — returns `204`.

**`POST /api/chat`** — request `{"message": "Buy 5 NVDA"}`; response:
```json
{
  "message": "Done — bought 5 NVDA at $890.10. NVDA is now 31% of your portfolio.",
  "actions": [
    {"type": "trade", "ticker": "NVDA", "side": "buy", "quantity": 5, "status": "executed",
     "price": 890.10, "error": null},
    {"type": "watchlist", "ticker": "PYPL", "action": "add", "status": "failed",
     "error": "PYPL is already on the watchlist"}
  ]
}
```

**`GET /api/chat/history`** — most recent 50 messages, oldest first:
```json
[{"id": "…", "role": "user", "content": "Buy 5 NVDA", "actions": null,
  "created_at": "2026-11-16T14:03:20Z"}, ...]
```

---

## 9. Portfolio & Trade Rules

- **Fill price**: the latest price in the `PriceCache` at the moment the request is processed. The client never supplies a price. If the cache has no price for the ticker, the trade is rejected with `400 "No price available for XYZ yet"`.
- **Trading a ticker not on the watchlist**: allowed. The ticker is added to the watchlist first (and to the market data source), then the trade is attempted — so in Massive mode a brand-new ticker typically fails with "no price yet" until the next poll, and the user can retry.
- **Quantity**: must be `> 0`. Quantities are rounded to 4 decimal places; the minimum is `0.0001`.
- **Buys**: require `cash_balance >= quantity × price`. New average cost = `(old_qty × old_avg + qty × price) / (old_qty + qty)`.
- **Sells**: require `quantity <= held quantity` (with a `1e-6` tolerance so "sell all" always works). Average cost is unchanged. If the remaining quantity is below `1e-6`, the position row is deleted. Realized P&L is not tracked separately — it is reflected in cash.
- **Atomicity**: each trade (cash update, position upsert/delete, trade log insert, snapshot insert) runs in a single SQLite transaction. Multiple trades from one chat response execute sequentially, each validated against the state left by the previous one.
- **Watchlist cap**: max 30 tickers; adding beyond that returns `400`.
- **Portfolio value** = cash + Σ(quantity × current price).

---

## 10. LLM Integration

When writing code to make calls to LLMs, use the `cerebras` skill to use LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider. Structured Outputs should be used to interpret the results.

There is an OPENROUTER_API_KEY in the .env file in the project root.

### How It Works

When the user sends a chat message, the backend:

1. Persists the user message to `chat_messages`
2. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
3. Loads the most recent 20 messages from `chat_messages` as conversation history
4. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
5. Calls the LLM via LiteLLM → OpenRouter, requesting structured output (timeout: 30s)
6. Parses the complete structured JSON response into a Pydantic model
7. Auto-executes any trades and watchlist changes, recording a result for each (executed or failed + error)
8. Stores the assistant message and the action results in `chat_messages`
9. Returns `{message, actions}` to the frontend (no token-by-token streaming — Cerebras inference is fast enough that a loading indicator is sufficient)

There is a single LLM call per user message. The LLM writes its message *before* actions execute, so the LLM cannot comment on failures in the same turn. Instead, the frontend renders each action result inline beneath the message (e.g. "✓ Bought 5 NVDA @ $890.10" / "✗ Sell 100 TSLA failed: you hold 20"). Because results are stored in `chat_messages.actions` and included in the history sent on the next turn, the LLM sees what actually happened and can follow up.

### Structured Output Schema

Defined once as a Pydantic model and used both as the LLM `response_format` and for validation:

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

- `message` (required, string): The conversational text shown to the user
- `trades` (array, may be empty): `side` is the enum `"buy" | "sell"`, `quantity` is a number `> 0`. Each trade goes through exactly the same validation as manual trades (§9)
- `watchlist_changes` (array, may be empty): `action` is the enum `"add" | "remove"`

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:
- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

### Failure Handling

If the LLM call times out or errors, the API key is missing, or the response fails schema validation, `/api/chat` still returns `200` with a friendly assistant message (e.g. "Sorry, I couldn't reach the AI service — please try again.") and `actions: []`. The error is logged server-side and nothing is executed. The assistant message is persisted like any other.

### System Prompt Guidance

The LLM should be prompted as "FinAlly, an AI trading assistant" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven in responses
- Always respond with valid structured JSON

### LLM Mock Mode

When `LLM_MOCK=true`, the backend skips OpenRouter and returns deterministic responses based on simple pattern matching of the user message (case-insensitive):

| User message matches | Mock response |
|---|---|
| `buy <qty> <TICKER>` | `message: "Buying <qty> <TICKER>."`, one buy trade |
| `sell <qty> <TICKER>` | `message: "Selling <qty> <TICKER>."`, one sell trade |
| `watch <TICKER>` | `message: "Adding <TICKER> to your watchlist."`, one add change |
| `unwatch <TICKER>` | `message: "Removing <TICKER> from your watchlist."`, one remove change |
| anything else | `message: "This is a mock response from FinAlly."`, no actions |

Mock responses go through the same execution, persistence, and response path as real ones. This enables fast, free, reproducible E2E tests, development without an API key, and CI pipelines.

---

## 11. Frontend Design

### Layout

The frontend is a single-page application (one route, `/`) with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), session change % (§6), a sparkline mini-chart (accumulated from SSE since page load), and a remove control. An input to add a ticker.
- **Main chart area** — larger chart for the currently selected ticker, showing price over time accumulated from SSE since page load. Clicking a ticker in the watchlist selects it here. Defaults to the first watchlist ticker.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by unrealized P&L % (green = profit, red = loss)
- **P&L chart** — line chart showing total portfolio value over time, using `/api/portfolio/history`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill. API errors are shown inline next to the bar.
- **AI chat panel** — docked/collapsible sidebar. Loads prior messages from `/api/chat/history` on mount. Message input, scrolling conversation history, loading indicator while waiting for LLM response. Action results shown inline beneath each assistant message as confirmations (✓) or failures (✗).
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Data Flow

- **Prices**: one `EventSource` on `/api/stream/prices`. Each event replaces the client's latest-price map and appends to per-ticker history buffers (capped at the last 500 points per ticker) used by sparklines and the main chart.
- **Portfolio**: fetched from `/api/portfolio` on load and after every trade, watchlist change, or chat response with actions. Live values (header total, positions table, heatmap) are recomputed client-side as `quantity × latest SSE price`, so they tick in real time without polling.
- **P&L history**: fetched on load, after each trade, and refreshed every 30s.
- **Watchlist**: fetched from `/api/watchlist` on load and after add/remove (manual or via chat).
- **Connection status**: green on `EventSource` `open`; yellow while `readyState === CONNECTING` after an error; red if no successful reconnect within ~10s.

### Technical Notes

- Use **Recharts** for all charts (sparklines, main price chart, P&L line chart, and the treemap heatmap) — one dependency covers everything. Disable Recharts animations for streaming charts to keep updates cheap.
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed. For local dev, Next.js `rewrites` (dev server only) can proxy `/api` to `localhost:8000`.
- Tailwind CSS for styling with a custom dark theme

---

## 12. Docker & Deployment

### Multi-Stage Dockerfile

```
Stage 1: Node 20 slim
  - Copy frontend/
  - npm ci && npm run build (produces static export in out/)

Stage 2: Python 3.12 slim
  - Install uv
  - Copy backend/
  - uv sync --frozen (install Python dependencies from lockfile)
  - Copy frontend build output into /app/static
  - Expose port 8000
  - CMD: uvicorn serving FastAPI app on 0.0.0.0:8000
```

FastAPI registers all `/api` routers first, then mounts `StaticFiles(directory="static", html=True)` at `/`. Because the frontend is a single route, no SPA fallback logic is needed.

### Data Persistence

The project's `db/` directory is bind-mounted into the container:

```bash
docker run -d --name finally -v "$(pwd)/db:/app/db" -p 8000:8000 --env-file .env finally
```

The backend writes `/app/db/finally.db`, which appears on the host as `db/finally.db`. Students can inspect it with any SQLite tool, and reset the app by stopping the container and deleting the file.

### Start/Stop Scripts

**`scripts/start.sh`** (macOS/Linux):
- Builds the Docker image if not already built (or if `--build` flag passed)
- Removes any existing `finally` container, then runs a new one with the bind mount, port mapping, and `.env` file
- Prints the URL to access the app
- Optionally opens the browser

**`scripts/stop.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT touch `db/` (data persists)

**`scripts/start.ps1`** / **`scripts/stop.ps1`**: PowerShell equivalents for Windows (using `${PWD}\db` for the mount).

All scripts should be idempotent — safe to run multiple times.

### Optional Cloud Deployment

The container is designed to deploy to AWS App Runner, Render, or any container platform. A Terraform configuration for App Runner may be provided in a `deploy/` directory as a stretch goal, but is not part of the core build.

> **Warning:** there is no authentication. A publicly deployed instance lets anyone trade and spend the owner's OpenRouter credits. Only deploy behind access control or for short demos.

---

## 13. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**:
- Market data: simulator generates valid prices, GBM math is correct, Massive API response parsing works, both implementations conform to the abstract interface, `session_open` is recorded on first write (done, except `session_open`)
- Portfolio: trade execution logic, weighted average cost, P&L calculations, edge cases (selling more than owned, buying with insufficient cash, selling at a loss, selling the full position deletes the row, fractional rounding, no cached price)
- Tracked tickers: removing a watched ticker with an open position keeps it in the market data source
- LLM: structured output parsing handles all valid schemas, graceful handling of malformed responses and LLM errors, trade validation within chat flow, mock-mode pattern matching
- API routes: correct status codes (§8 conventions), response shapes, error handling

**Frontend (React Testing Library or similar)**:
- Component rendering with mock data
- Price flash animation triggers correctly on price changes
- Watchlist CRUD operations
- Portfolio display calculations
- Chat message rendering, action result rendering, and loading state

### E2E Tests (in `test/`)

**Infrastructure**: `test/docker-compose.test.yml` spins up the app container plus a Playwright container. This keeps browser dependencies out of the production image and off the host.

**Environment**: Tests run with `LLM_MOCK=true` and a fresh, throwaway database (no bind mount) for speed and determinism.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell shares: cash increases, position updates or disappears
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send "buy 1 AAPL", receive a response, trade execution appears inline and the position appears
- SSE resilience: disconnect and verify reconnection
