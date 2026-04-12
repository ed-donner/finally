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
- Uptick Green: `#00c853`
- Downtick Red: `#ff1744`

## 3. Architecture Overview

### Single Container, Single Port

```text
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


| Decision                | Rationale                                                                                     |
| ----------------------- | --------------------------------------------------------------------------------------------- |
| SSE over WebSockets     | One-way push is all we need; simpler, no bidirectional complexity, universal browser support  |
| Static Next.js export   | Single origin, no CORS issues, one port, one container, simple deployment                     |
| SQLite over Postgres    | No auth = no multi-user = no need for a database server; self-contained, zero config          |
| Single Docker container | Students run one command; no docker-compose for production, no service orchestration          |
| uv for Python           | Fast, modern Python project management; reproducible lockfile; what students should learn     |
| Market orders only      | Eliminates order book, limit order logic, partial fills — dramatically simpler portfolio math |


---

## 4. Directory Structure

```text
finally/
├── frontend/                 # Next.js TypeScript project (static export)
├── backend/                  # FastAPI uv project (Python)
│   └── schema/               # SQL schema definitions, seed data, migration logic
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start.sh              # Launch Docker container (bash — macOS/Linux/WSL/Git Bash)
│   └── stop.sh               # Stop Docker container
├── test/                     # Playwright E2E tests + docker-compose.test.yml
├── db/                       # Volume mount target (SQLite file lives here at runtime)
│   └── .gitkeep              # Directory exists in repo; finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── .env                      # Environment variables (gitignored)
├── .env.example              # Template with placeholder values (committed)
└── .gitignore
```

### Key Boundaries

- `**frontend/**` is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- `**backend/**` is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- `**backend/schema/**` contains SQL schema definitions and seed logic. Named `schema/` (not `db/`) to avoid confusion with the runtime `db/` directory.
- `**db/**` at the top level is the runtime volume mount point. The SQLite file (`db/finally.db`) is created here by the backend and persists across container restarts via Docker volume. Inside the container, this maps to `/app/db/finally.db`.
- `**planning/**` contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- `**test/**` contains Playwright E2E tests and supporting infrastructure (e.g., `docker-compose.test.yml`). Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- `**scripts/**` contains bash start/stop scripts that wrap Docker commands. Bash-only (works on macOS, Linux, WSL, and Git Bash on Windows).

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

A `.env.example` file with the above placeholder values is committed to the repo. The actual `.env` file is gitignored.

### Behavior

- If `MASSIVE_API_KEY` is set and non-empty → backend uses Massive REST API for market data
- If `MASSIVE_API_KEY` is absent or empty → backend uses the built-in market simulator
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses (for E2E tests)
- The backend reads `.env` from the project root (mounted into the container or read via docker `--env-file`)

---

## 6. Market Data

### Two Implementations, One Interface

Both the simulator and the Massive client implement the same abstract interface. The backend selects which to use based on the `MASSIVE_API_KEY` environment variable. All downstream code (SSE streaming, price cache, frontend) is agnostic to the source.

### Simulator (Default)

- Generates prices using geometric Brownian motion (GBM) with configurable drift and volatility per ticker
- Updates at ~500ms intervals
- Correlated moves across tickers (e.g., tech stocks move together)
- Occasional random "events" — sudden 2-5% moves on a ticker for drama
- Starts from realistic seed prices (e.g., AAPL ~$190, GOOGL ~$175, etc.)
- Records each ticker's seed price as its `prev_close` — used as a **session baseline** for change % calculation. This value is fixed for the lifetime of the process (resets on container restart). It does not roll over at market close. This is a deliberate simplification: `day_change` and `day_change_pct` in the SSE stream represent change since process start, not true daily change. This is acceptable because the simulator itself resets on restart, so there is no meaningful "prior day."
- When a new ticker is added at runtime, the simulator looks up a seed price from its built-in table. If the ticker is not in the table, it starts from a default price (e.g., $100).
- Runs as an in-process background task — no external dependencies

### Massive API (Optional)

Massive (formerly Polygon.io, rebranded October 2025) provides real-time and historical stock data via REST.

- **Base URL**: `https://api.massive.com`
- **Authentication**: API key passed as query parameter — `?apiKey=YOUR_KEY`
- **Quotes endpoint**: `GET /v3/quotes/{stockTicker}?limit=1&apiKey=...` — returns the latest quote for a single ticker. There is no batch endpoint, so the poller makes one request per tracked ticker.
- **Previous close endpoint**: `GET /v2/aggs/ticker/{stockTicker}/prev?apiKey=...` — returns the previous day's OHLCV bar. Used once on startup (and once per newly added ticker) to populate `prev_close`. Unlike the simulator, Massive mode uses true prior-day close. However, this value is still fetched once and not refreshed — in a long-running container spanning multiple trading days, `prev_close` will become stale. This is an accepted limitation; restarting the container resets it.
- **Example quote response**:
  ```json
  {
    "status": "OK",
    "results": [
      {
        "bid_price": 190.25,
        "ask_price": 190.27,
        "bid_size": 2,
        "ask_size": 3,
        "participant_timestamp": 1680000000000000000,
        "sip_timestamp": 1680000000001000000
      }
    ]
  }
  ```
- **Price derivation**: Use the midpoint of `bid_price` and `ask_price` as the current price
- **Polling cadence & rate limiting**: The free tier allows 5 API calls per minute total. With no batch endpoint, the poller uses round-robin: one ticker per polling cycle, rotating through all tracked tickers. With 10 tickers at 5 calls/min, each ticker updates roughly every 2 minutes. Tickers display their most recent known price between updates. Paid tiers (higher rate limits) can poll more aggressively — every 2-5 seconds per ticker.
- **Tracked ticker cap (free tier)**: To prevent staleness from degrading the experience, the total number of tracked tickers (watchlist + open positions) is capped at 25 in Massive mode on the free tier. Adding a ticker beyond this cap returns a `400` error explaining that the limit has been reached. This ensures each ticker is polled at least once within the 5-minute staleness window. The cap does not apply in simulator mode.

### Ticker Lifecycle & Price Cache

The active data source (simulator or Massive poller) generates prices for the **union of watchlist tickers and open position tickers**. This ensures that held positions always have a current price for P&L, heatmap, and portfolio value calculations — even if removed from the watchlist, or if the AI trades a ticker not on the watchlist.

- When a ticker is added to the watchlist or a position is opened, the data source begins tracking it
- When a ticker is removed from the watchlist **and** the user has no open position in it, the data source stops tracking it and it is removed from the price cache
- An in-memory price cache holds the latest price, previous price, previous close (for daily change), and timestamp for each tracked ticker
- The SSE stream pushes updates for all tracked tickers (watchlist + held positions)
- The frontend uses watchlist membership to decide what to show in the watchlist panel; the SSE stream may include additional tickers for position pricing

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- Server pushes price updates for all tracked tickers at a regular cadence (~500ms)
- Each SSE event contains: `ticker`, `price`, `prev_price` (previous tick), `prev_close` (session/day open baseline), `day_change` (price - prev_close), `day_change_pct` ((price - prev_close) / prev_close  100), `timestamp`, and `direction` (`"up"`, `"down"`, or `"flat"`)
- Client handles reconnection automatically (EventSource has built-in retry)

---

## 7. Database

### SQLite with Lazy Initialization

The backend checks for the SQLite database on startup (or first request). If the file doesn't exist or tables are missing, it creates the schema and seeds default data. The database path inside the container is `/app/db/finally.db`. This means:

- No separate migration step
- No manual database setup
- Fresh Docker volumes start with a clean, seeded database automatically
- Single uvicorn worker assumed — no concurrent write issues

### Schema

All tables include a `user_id` column defaulting to `"default"`. This is hardcoded for now (single-user) but enables future multi-user support without schema migration.

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

**positions** — Current holdings (one row per ticker per user). When a sell reduces quantity to zero, the row is deleted. This keeps the positions table, heatmap, and portfolio display clean.

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

**portfolio_snapshots** — Portfolio value over time (for P&L chart). Recorded every 10 seconds by a background task, and immediately after each trade execution. Retention: keep only the last 24 hours of snapshots; a periodic cleanup task prunes older rows.

- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp)

**chat_messages** — Conversation history with LLM

- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON — trades executed with results including errors, watchlist changes made; null for user messages)
- `created_at` TEXT (ISO timestamp)

### Portfolio Value Formulas

- **Total portfolio value**: `cash_balance + sum(position.quantity * current_price)` for all open positions
- **Unrealized P&L per position**: `quantity * (current_price - avg_cost)`
- **Unrealized P&L %**: `(current_price - avg_cost) / avg_cost * 100`
- **Average cost update on buy**: `new_avg_cost = (old_quantity * old_avg_cost + buy_quantity * buy_price) / (old_quantity + buy_quantity)`
- **Average cost on sell**: unchanged (selling does not affect avg_cost)

### Default Seed Data

- One user profile: `id="default"`, `cash_balance=10000.0`
- Ten watchlist entries: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX

---

## 8. API Endpoints

### Market Data


| Method | Path                 | Description                      |
| ------ | -------------------- | -------------------------------- |
| GET    | `/api/stream/prices` | SSE stream of live price updates |


### Portfolio


| Method | Path                     | Description                                                            |
| ------ | ------------------------ | ---------------------------------------------------------------------- |
| GET    | `/api/portfolio`         | Current positions, cash balance, total value, unrealized P&L           |
| POST   | `/api/portfolio/trade`   | Execute a trade: `{ticker, quantity, side}` — quantity is whole shares |
| GET    | `/api/portfolio/history` | Portfolio value snapshots over time (for P&L chart)                    |
| POST   | `/api/portfolio/reset`   | Full reset (see details below)                                         |


### Watchlist


| Method | Path                      | Description                                  |
| ------ | ------------------------- | -------------------------------------------- |
| GET    | `/api/watchlist`          | Current watchlist tickers with latest prices |
| POST   | `/api/watchlist`          | Add a ticker: `{ticker}`                     |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker                              |


### Chat


| Method | Path        | Description                                                                                 |
| ------ | ----------- | ------------------------------------------------------------------------------------------- |
| POST   | `/api/chat` | Send a message: `{"message": "string"}`, receive JSON response (message + executed actions) |


### System


| Method | Path          | Description                                             |
| ------ | ------------- | ------------------------------------------------------- |
| GET    | `/api/health` | Health check — returns `{"status": "ok"}` with HTTP 200 |


### Standard Error Response

All API endpoints return errors in a consistent format:

```json
{ "error": "Short error description", "detail": "More specific information" }
```

HTTP status codes: `400` for validation errors (insufficient cash, invalid ticker, etc.), `404` for unknown resources, `500` for server errors.

### Success Response Schemas

**SSE event** (`GET /api/stream/prices`) — each event is a JSON object:

```json
{
  "ticker": "AAPL",
  "price": 191.42,
  "prev_price": 191.38,
  "prev_close": 190.0,
  "day_change": 1.42,
  "day_change_pct": 0.747,
  "timestamp": "2026-04-01T14:30:00.123Z",
  "direction": "up"
}
```

`**GET /api/portfolio**` — HTTP 200:

```json
{
  "cash_balance": 8500.0,
  "total_value": 10234.5,
  "positions": [
    {
      "ticker": "AAPL",
      "quantity": 10,
      "avg_cost": 190.0,
      "current_price": 191.42,
      "unrealized_pnl": 14.2,
      "unrealized_pnl_pct": 0.747
    }
  ]
}
```

`**POST /api/portfolio/trade**` — HTTP 200. Request: `{"ticker": "AAPL", "quantity": 10, "side": "buy"}`. Response:

```json
{
  "trade": {
    "id": "uuid",
    "ticker": "AAPL",
    "side": "buy",
    "quantity": 10,
    "price": 191.42,
    "executed_at": "2026-04-01T14:30:00.123Z"
  },
  "cash_balance": 6585.8
}
```

`**GET /api/portfolio/history**` — HTTP 200:

```json
{
  "snapshots": [
    { "total_value": 10000.0, "recorded_at": "2026-04-01T14:00:00Z" },
    { "total_value": 10050.25, "recorded_at": "2026-04-01T14:00:10Z" }
  ]
}
```

`**POST /api/portfolio/reset**` — HTTP 200:

```json
{ "status": "ok" }
```

`**GET /api/watchlist**` — HTTP 200:

```json
{
  "watchlist": [
    {
      "ticker": "AAPL",
      "price": 191.42,
      "day_change": 1.42,
      "day_change_pct": 0.747,
      "direction": "up"
    }
  ]
}
```

`**POST /api/watchlist**` — HTTP 200. Request: `{"ticker": "PYPL"}`. Response:

```json
{ "ticker": "PYPL", "added_at": "2026-04-01T14:30:00Z" }
```

`**DELETE /api/watchlist/{ticker}**` — HTTP 200:

```json
{ "status": "ok" }
```

`**POST /api/chat**` — HTTP 200. Request: `{"message": "Buy 5 shares of AAPL"}`. Response:

```json
{
  "message": "Done! I bought 5 shares of AAPL at $191.42.",
  "actions": {
    "trades": [
      {
        "ticker": "AAPL",
        "side": "buy",
        "quantity": 5,
        "success": true,
        "price": 191.42,
        "error": null
      }
    ],
    "watchlist_changes": [
      {
        "ticker": "PYPL",
        "action": "add",
        "success": true,
        "error": null
      }
    ]
  }
}
```

The `actions` field contains the post-execution results of trades and watchlist changes requested by the LLM, with per-action `success` and `error` fields. This is what gets stored in the `chat_messages.actions` column.

### Portfolio Reset Behavior

`POST /api/portfolio/reset` restores the account to a clean initial state. Specifically:

- **Cleared**: `positions`, `trades`, `portfolio_snapshots`, `chat_messages` — all rows for the user are deleted
- **Reset**: `users_profile.cash_balance` set back to `10000.0`
- **Preserved**: `watchlist` is reset to the 10 default tickers (any custom tickers removed, any missing defaults re-added)
- The price cache and simulator continue running — no interruption to the SSE stream

### Trade Execution Semantics

- **Current price**: The latest price in the in-memory price cache for that ticker. This is the fill price — no slippage, no spread.
- **On-demand price seeding**: If a trade is requested for a ticker not currently in the price cache (e.g., the AI trades an off-watchlist ticker), the backend seeds it on demand before validating the trade. In simulator mode, this is instant — the simulator generates a price from its seed table. In Massive mode, the backend makes a synchronous quote request to fetch the current price and adds it to the cache before proceeding. This ensures "trade any ticker" works reliably regardless of data source.
- **Staleness guard**: A trade is rejected if the ticker's cached price timestamp is older than 5 seconds (simulator) or 5 minutes (Massive mode, due to round-robin polling). After the on-demand seeding step above, the price will always be fresh, so this guard primarily catches trades on tickers that are tracked but whose poll cycle hasn't run recently. The error message should indicate the price is unavailable.
- **Concurrency**: All trade execution must be serialized via a Python `asyncio.Lock`. This prevents double-spending when a manual trade and an LLM-initiated trade arrive simultaneously. The lock covers the read-validate-write cycle (check cash/shares → execute → update balances). Single uvicorn worker assumed.
- **Multi-action chat responses**: When the LLM returns multiple trades and/or watchlist changes, they execute **best-effort, one at a time** — not atomically. Watchlist changes execute first (so that new tickers can begin pricing), then trades execute in array order. Each action succeeds or fails independently; failures are collected and reported in the chat response.

---

## 9. LLM Integration

### LLM Provider Contract

The backend calls the LLM via **LiteLLM** using OpenRouter as the provider and Cerebras for inference. Concrete details:

- **Python package**: `litellm`
- **Model string**: `"openrouter/openai/gpt-oss-120b"`
- **API key**: Read `OPENROUTER_API_KEY` from environment / `.env`
- **LiteLLM call**: Use `litellm.completion()` with `response_format` set to the structured output JSON schema (see below)
- **Extra headers**: Pass `{"X-Title": "FinAlly"}` via `extra_headers` for OpenRouter analytics

Example call pattern:

```python
import litellm

response = litellm.completion(
    model="openrouter/openai/gpt-oss-120b",
    messages=messages,
    response_format={"type": "json_schema", "json_schema": {...}},
    api_key=os.environ["OPENROUTER_API_KEY"],
    extra_headers={"X-Title": "FinAlly"},
)
```

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads the last 20 messages from `chat_messages` (capped to avoid exceeding LLM context limits)
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via `litellm.completion()` with structured output
5. Parses the complete structured JSON response
6. Auto-executes watchlist changes first, then trades in order (see Trade Execution Semantics above)
7. Stores the message and executed actions (including per-action success/failure) in `chat_messages`
8. Returns the complete JSON response to the frontend (no token-by-token streaming — Cerebras inference is fast enough that a loading indicator is sufficient)

### Structured Output Schema

The LLM is instructed to respond with JSON matching this schema:

```json
{
  "message": "Your conversational response to the user",
  "trades": [{ "ticker": "AAPL", "side": "buy", "quantity": 10 }],
  "watchlist_changes": [{ "ticker": "PYPL", "action": "add" }]
}
```

- `message` (required): The conversational text shown to the user
- `trades` (optional): Array of trades to auto-execute. Quantity must be a whole number (integer). Each trade goes through the same validation as manual trades (sufficient cash for buys, sufficient shares for sells)
- `watchlist_changes` (optional): Array of watchlist modifications. Valid `action` values: `"add"` and `"remove"`. Idempotent — adding a ticker already on the watchlist is a no-op, removing a ticker not on the watchlist is a no-op.

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:

- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

If a trade fails validation (e.g., insufficient cash), the error is included in the chat response so the LLM can inform the user.

### Error Handling

If the LLM call fails (network error, timeout) or returns a response that cannot be parsed as valid structured JSON, the backend should:

1. Retry up to 2 additional times (3 attempts total)
2. If all attempts fail: not execute any trades or watchlist changes
3. Return a chat response with a user-friendly error message (e.g., "Sorry, I'm having trouble responding right now. Please try again.")
4. Not store the failed response in `chat_messages` — the user's message is still stored so they can see what they sent

### System Prompt Guidance

The LLM should be prompted as "FinAlly, an AI trading assistant" with instructions to:

- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven in responses
- Always respond with valid structured JSON

### LLM Mock Mode

When `LLM_MOCK=true`, the backend returns deterministic mock responses instead of calling OpenRouter. Mock responses should include at least one trade and one watchlist change to enable E2E testing of the auto-execution flow. This enables:

- Fast, free, reproducible E2E tests
- Development without an API key
- CI/CD pipelines

---

## 10. Frontend Design

### Layout

The frontend is a single-page application with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), daily change %, a sparkline mini-chart (accumulated from SSE since page load), and inline buy/sell buttons with a quantity input. Clicking a ticker row selects it in the main chart. Trading directly from the watchlist eliminates the need for a separate trade bar and ticker input validation.
- **Main chart area** — larger chart for the currently selected ticker, with at minimum price over time.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss)
- **P&L chart** — line chart showing total portfolio value over time, using data from `portfolio_snapshots`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **AI chat panel** — docked/collapsible sidebar. Message input, scrolling conversation history, loading indicator while waiting for LLM response. Trade executions and watchlist changes shown inline as confirmations. The AI chat can also execute trades on any ticker (including tickers not on the watchlist).
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`
- Canvas-based charting library preferred (e.g., Lightweight Charts) or SVG-based (e.g., Recharts) for performance
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- All API calls go to the same origin (`/api/`*) — no CORS configuration needed
- Tailwind CSS for styling with a custom dark theme

---

## 11. Docker & Deployment

### Multi-Stage Dockerfile

```text
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

The SQLite database persists via a **named Docker volume** called `finally-data`:

```bash
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally
```

The named volume `finally-data` is mounted at `/app/db` inside the container. The backend writes `finally.db` to this path. The repo-level `db/` directory (with `.gitkeep`) exists only to document the mount point — it is **not** used as a bind mount. To inspect the live SQLite file, use `docker cp` or `docker exec`, not the local `db/` directory.

### Start/Stop Scripts

`**scripts/start.sh`** (bash — macOS/Linux/WSL/Git Bash on Windows):

- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container with the volume mount, port mapping, and `.env` file
- Prints the URL to access the app
- Optionally opens the browser

`**scripts/stop.sh`**:

- Stops and removes the running container
- Does NOT remove the volume (data persists)

Both scripts should be idempotent — safe to run multiple times. No separate Windows PowerShell scripts; bash via WSL or Git Bash covers Windows users.

### Optional Cloud Deployment

The container is designed to deploy to AWS App Runner, Render, or any container platform. A Terraform configuration for App Runner may be provided in a `deploy/` directory as a stretch goal, but is not part of the core build.

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
- Sell to zero: position row disappears from positions table and heatmap
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send a message, receive a response, trade execution appears inline
- AI chat watchlist changes: LLM adds/removes a ticker, watchlist updates accordingly
- SSE resilience: disconnect and verify reconnection
- Off-watchlist position pricing: buy a ticker via AI chat that is not on the watchlist, verify it has a live price in the positions table and heatmap
- Watchlist removal with open position: remove a ticker from the watchlist while holding shares, verify the position still shows a current price and P&L
- Portfolio reset: execute some trades and chat, then reset — verify $10k cash, no positions, empty trade history, P&L chart cleared, chat history cleared, default watchlist restored
- Stale/missing price trade rejection: attempt a trade on a ticker with no cached price, verify it is rejected with a clear error

