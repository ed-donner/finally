# FinAlly — AI Trading Workstation

## Project Specification

## 1. Vision

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions, recommend trades, and execute approved trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

## 2. User Experience

### First Launch

The user runs a single Docker command (or a provided start script). A browser opens to `http://localhost:8000`. No login, no signup. They immediately see:

- A watchlist of 10 default tickers with live-updating prices in a grid
- $10,000 in virtual cash
- A dark, data-rich trading terminal aesthetic
- An AI chat panel ready to assist
- If no LLM credentials are configured, chat still works in deterministic mock mode instead of appearing broken

### What the User Can Do

- **Watch prices stream** — prices flash green (uptick) or red (downtick) with subtle CSS animations that fade
- **View sparkline mini-charts** — price action beside each ticker in the watchlist, accumulated on the frontend from the SSE stream since page load (sparklines fill in progressively)
- **Click a ticker** to see a larger detailed chart in the main chart area
- **Buy and sell shares** — market orders only, instant fill at current price, no fees, no confirmation dialog
- **Monitor their portfolio** — a heatmap (treemap) showing positions sized by weight and colored by P&L, plus a P&L chart tracking total portfolio value over time
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, % change
- **Chat with the AI assistant** — ask about their portfolio, get analysis, and have the AI recommend or execute approved trades and manage the watchlist through natural language
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
- **AI integration**: LiteLLM → OpenRouter, with structured outputs for trade recommendations and approved execution
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
├── db/                       # Volume mount target (SQLite file lives here at runtime)
│   └── .gitkeep              # Directory exists in repo; finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── docker-compose.yml        # Optional convenience wrapper
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/db/`** contains schema SQL definitions and seed logic. The backend lazily initializes the database on first request — creating tables and seeding default data if the SQLite file doesn't exist or is empty.
- **`db/`** at the top level is the runtime volume mount point. The SQLite file (`db/finally.db`) is created here by the backend and persists across container restarts via Docker volume.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests and supporting infrastructure (e.g., `docker-compose.test.yml`). Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- **`scripts/`** contains start/stop scripts that wrap Docker commands.

---

## 5. Environment Variables

```bash
# Optional but recommended: OpenRouter API key for live LLM chat
# If omitted, chat falls back to deterministic mock mode automatically
OPENROUTER_API_KEY=your-openrouter-api-key-here

# Optional: Massive API key for real market data
# If not set, the built-in market simulator is used (recommended for most users)
MASSIVE_API_KEY=

# Optional: Set to "true" for deterministic mock LLM responses (testing)
LLM_MOCK=false

# Optional: Integer seed for deterministic simulator price paths (E2E tests)
SIMULATOR_SEED=
```

### Behavior

- If `MASSIVE_API_KEY` is set and non-empty → backend uses Massive REST API for market data
- If `MASSIVE_API_KEY` is absent or empty → backend uses the built-in market simulator
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses (for E2E tests)
- If `LLM_MOCK` is not enabled but `OPENROUTER_API_KEY` is absent or empty → backend falls back to deterministic mock responses instead of failing chat requests
- If `SIMULATOR_SEED` is set to an integer → the simulator seeds its RNG so price paths are reproducible across runs (intended for E2E determinism). If unset, prices are non-deterministic.
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
- The cache holds the latest price, previous price, change, change percent, direction, and timestamp for each ticker
- SSE streams read from this cache and push updates to connected clients
- The current implementation is still single-user. The cache and schema leave room for future multi-user work, but auth, session isolation, and per-user streaming would still require additional application changes.

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- The simulator/poller updates the price cache at its native cadence (~500ms for the simulator, the configured polling interval for Massive). The SSE endpoint pushes an event whenever the cache version advances for a ticker — on change, not on a fixed heartbeat.
- Each SSE event contains ticker, price, previous price, change (delta), change percent, direction, and timestamp
- Client handles reconnection automatically (EventSource has built-in retry)

---

## 7. Database

### SQLite with Lazy Initialization

The backend checks for the SQLite database on startup (or first request). If the file doesn't exist or tables are missing, it creates the schema and seeds default data. This means:

- No separate migration step
- No manual database setup
- Fresh Docker volumes start with a clean, seeded database automatically

### Schema

All user-scoped tables include a `user_id` column defaulting to `"default"`. This is hardcoded for now (single-user) and keeps the door open for later expansion, but does not by itself make the app multi-user-ready.

**user_profile** — User state (cash balance)
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
- `quantity` REAL (fractional shares supported to 4 decimal places; minimum `0.0001`)
- `price` REAL
- `executed_at` TEXT (ISO timestamp)

**portfolio_snapshots** — Portfolio value over time (for P&L chart). One initial snapshot is recorded during first-run seed so the chart has data immediately. Additional snapshots are recorded every 30 seconds by a background task and immediately after each successful trade execution. A retention task keeps full 30-second resolution for the last 24 hours, downsamples older data to 5-minute buckets, and deletes rows older than 30 days.
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp)

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON — trade recommendations, approval-required markers, executed trades, failed trades, and watchlist changes; null for user messages)
- `created_at` TEXT (ISO timestamp)

### Default Seed Data

- One user profile: `id="default"`, `cash_balance=10000.0`
- Ten watchlist entries: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX
- One initial portfolio snapshot at `$10,000.00`

---

## 8. API Endpoints

All JSON endpoints use these conventions:

- Success: `200` or `201`
- Successful delete: `204` with no body
- Domain validation errors: `400`
- Missing resources: `404`
- Watchlist duplicates: `409`
- Request-body validation failures before route logic: `422`

Standard error payload:

```json
{"detail": "human-readable error message"}
```

### Market Data
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/stream/prices` | SSE stream of live price updates |

Example SSE payload:

```json
{
  "ticker": "AAPL",
  "price": 191.42,
  "previous_price": 191.05,
  "timestamp": 1776321000.12,
  "change": 0.37,
  "change_percent": 0.1937,
  "direction": "up"
}
```

`change_percent` is tick-to-tick movement from the previous cached quote, not day-over-day session change.

### Portfolio
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/portfolio` | Current positions, cash balance, total value, unrealized P&L |
| POST | `/api/portfolio/trade` | Execute a trade: `{ticker, quantity, side}` |
| GET | `/api/portfolio/history` | Portfolio value snapshots over time (for P&L chart) |

`GET /api/portfolio` example:

```json
{
  "cash_balance": 9900.0,
  "positions": [
    {
      "ticker": "AAPL",
      "quantity": 1.0,
      "avg_cost": 100.0,
      "current_price": 101.5,
      "market_value": 101.5,
      "unrealized_pnl": 1.5,
      "unrealized_pnl_percent": 1.5
    }
  ],
  "market_value": 101.5,
  "total_value": 10001.5,
  "unrealized_pnl": 1.5,
  "unrealized_pnl_percent": 1.5
}
```

`POST /api/portfolio/trade` request:

```json
{
  "ticker": "AAPL",
  "side": "buy",
  "quantity": 1
}
```

Trade rules:

- `ticker` is normalized to uppercase and must be 1-10 alphanumeric characters
- `quantity` must be finite, at least `0.0001`, and have at most 4 decimal places
- only `buy` and `sell` are supported
- short selling is not supported
- selling to zero removes the position row
- the current quote must be fresh; stale prices are rejected

`POST /api/portfolio/trade` success response:

```json
{
  "ticker": "AAPL",
  "side": "buy",
  "quantity": 1.0,
  "price": 100.0,
  "cash_balance_after": 9900.0,
  "position_quantity_after": 1.0,
  "executed_at": "2026-04-17T06:00:00+00:00"
}
```

`GET /api/portfolio/history` example:

```json
[
  {"total_value": 10000.0, "recorded_at": "2026-04-17T05:59:30+00:00"},
  {"total_value": 10001.5, "recorded_at": "2026-04-17T06:00:00+00:00"}
]
```

### Watchlist
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Current watchlist tickers with latest prices |
| POST | `/api/watchlist` | Add a ticker: `{ticker}` |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker |

`GET /api/watchlist` example:

```json
[
  {
    "ticker": "AAPL",
    "price": 191.42,
    "previous_price": 191.05,
    "change": 0.37,
    "change_percent": 0.1937,
    "direction": "up"
  }
]
```

`POST /api/watchlist` request:

```json
{"ticker": "PYPL"}
```

Duplicate adds return `409`. Invalid tickers return `400`. `DELETE /api/watchlist/{ticker}` returns `204` on success and `404` when the ticker is not present.

### Chat
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/chat` | Recent conversation history, for rehydrating the thread on page reload |
| POST | `/api/chat` | Send a message, receive complete JSON response (message + action results) |

`GET /api/chat` details:

- `limit` defaults to `50`
- valid range is `1..500`
- results are returned oldest-first for direct rendering in the UI

`GET /api/chat` example:

```json
[
  {
    "id": "msg-1",
    "role": "user",
    "content": "Should I buy AAPL?",
    "actions": null,
    "created_at": "2026-04-17T06:00:00+00:00"
  },
  {
    "id": "msg-2",
    "role": "assistant",
    "content": "AAPL looks strong. No trades were executed.",
    "actions": {
      "trades": [
        {
          "ticker": "AAPL",
          "side": "buy",
          "quantity": 1.0,
          "intent": "recommend",
          "status": "recommended",
          "price": null,
          "cash_balance_after": null,
          "error": null
        }
      ],
      "watchlist_changes": []
    },
    "created_at": "2026-04-17T06:00:01+00:00"
  }
]
```

`POST /api/chat` request:

```json
{
  "message": "buy 1 AAPL",
  "allow_trade_execution": true
}
```

`allow_trade_execution` is the backend approval gate. If omitted or `false`, the backend may still return trade recommendations, but it will not execute them.

`POST /api/chat` example:

```json
{
  "user_message": {
    "id": "msg-3",
    "role": "user",
    "content": "buy 1 AAPL",
    "actions": null,
    "created_at": "2026-04-17T06:02:00+00:00"
  },
  "assistant_message": {
    "id": "msg-4",
    "role": "assistant",
    "content": "Buying 1 share of AAPL.\n\nTrade execution was not attempted because this message was not approved for execution.",
    "actions": {
      "trades": [
        {
          "ticker": "AAPL",
          "side": "buy",
          "quantity": 1.0,
          "intent": "execute",
          "status": "approval_required",
          "price": null,
          "cash_balance_after": null,
          "error": "Trade execution requires explicit approval for this message"
        }
      ],
      "watchlist_changes": []
    },
    "created_at": "2026-04-17T06:02:01+00:00"
  }
}
```

Trade action statuses:

- `recommended`: analysis only, nothing executed
- `approval_required`: the model asked to execute, but this request was not approved
- `executed`: trade filled successfully
- `failed`: execution was attempted but domain validation failed

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (for Docker/deployment) |

---

## 9. LLM Integration

The backend uses LiteLLM via OpenRouter against the `openrouter/openai/gpt-oss-120b` model with structured outputs. Provider routing is an implementation detail of the backend, not part of the product contract.

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads recent conversation history from the `chat_messages` table
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via LiteLLM → OpenRouter, requesting structured output
5. Parses the complete structured JSON response
6. Applies watchlist changes immediately and only executes trades when the incoming API request explicitly allows execution
7. Stores the message and executed actions in `chat_messages`
8. Returns the complete JSON response to the frontend (no token-by-token streaming — a standard loading indicator is sufficient)

### Structured Output Schema

The LLM is instructed to respond with JSON matching this schema:

```json
{
  "message": "Your conversational response to the user",
  "trades": [
    {"ticker": "AAPL", "side": "buy", "quantity": 10, "intent": "execute"}
  ],
  "watchlist_changes": [
    {"ticker": "PYPL", "action": "add"}
  ]
}
```

- `message` (required): The conversational text shown to the user
- `trades` (required, may be `[]`): Array of trades the model is recommending or attempting to execute. Each trade includes `intent`, which must be either `recommend` or `execute`. Even `execute` intents are still subject to the API request's `allow_trade_execution` flag plus the same validation as manual trades.
- `watchlist_changes` (required, may be `[]`): Array of `{ticker, action}` entries where `action` must be `"add"` or `"remove"`

Both action arrays are **required** in the structured output contract even when empty — structured outputs enforce the presence of all declared fields. The LLM returns `[]` to signal "no action".

### Execution Authority

Trade execution authority is enforced server-side, not by prompt wording alone:

- the model may emit `intent: "recommend"` or `intent: "execute"`
- the backend only executes trade intents when `allow_trade_execution=true` was sent with that specific chat request
- otherwise the backend returns `approval_required` action results and persists them in chat history for inline rendering

If an LLM-requested trade fails validation (e.g., insufficient cash, insufficient shares, stale quote), the backend does **not** re-call the LLM. Instead, the per-trade error is attached to that action in the response payload (and persisted in the `actions` field of the `chat_messages` row), and the frontend renders a failed-trade badge inline beneath the assistant's message. Successful trades and watchlist changes in the same response still apply.

### System Prompt Guidance

The LLM should be prompted as "FinAlly, an AI trading assistant" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Set `intent: "recommend"` for analysis or hypothetical ideas
- Set `intent: "execute"` only when the user clearly asks to place the order
- Manage the watchlist proactively
- Be concise and data-driven in responses

### LLM Mock Mode

When `LLM_MOCK=true`, the backend returns deterministic mock responses instead of calling OpenRouter. The backend also falls back to this mode automatically when no `OPENROUTER_API_KEY` is configured. This enables:
- Fast, free, reproducible E2E tests
- Development without an API key
- CI/CD pipelines

---

## 10. Frontend Design

### Layout

The frontend is a single-page application with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), tick-to-tick change %, and a sparkline mini-chart (accumulated from SSE since page load)
- **Main chart area** — larger chart for the currently selected ticker, with at minimum price over time. Clicking a ticker in the watchlist selects it here.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss)
- **P&L chart** — line chart showing total portfolio value over time, using data from `portfolio_snapshots`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill.
- **AI chat panel** — docked/collapsible sidebar. Message input, scrolling conversation history, loading indicator while waiting for LLM response, and an explicit "execute trades from this message" control. Trade recommendations, approvals-required states, executions, failures, and watchlist changes are shown inline beneath the assistant message.
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`
- TradingView Lightweight Charts (canvas-based) for streaming price charts and sparklines; Recharts (SVG) is acceptable for low-frequency charts like the P&L line chart
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed
- Tailwind CSS for styling with a custom dark theme
- **State management**: React Context + hooks is the default for shared state (SSE prices, watchlist, portfolio, chat). Only introduce a global store (Zustand, Redux) if component-tree complexity demands it.
- **Error surfaces**:
  - API / network errors → transient toast notifications (top-right), auto-dismissing
  - Chat-related errors (e.g., failed LLM-requested trades) → inline badges under the assistant's message in the chat thread, not toasts
  - SSE connection state → the header connection-status dot (green / yellow / red) is the single source of truth; no toasts for reconnects

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

The SQLite database persists via a host bind mount: the project's top-level `db/` directory is mounted into the container at `/app/db`. The backend writes `finally.db` to this path, so the database survives container restarts and is directly visible on the host.

```bash
docker run -v "$PWD/db:/app/db" -p 8000:8000 --env-file .env finally
```

### Start/Stop Scripts

**`scripts/start_mac.sh`** (macOS/Linux):
- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container with the volume mount, port mapping, and `.env` file
- Prints the URL to access the app
- May optionally open the browser when launched from a local wrapper script; raw `docker run` only prints the URL

**`scripts/stop_mac.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT remove the volume (data persists)

**`scripts/start_windows.ps1`** / **`scripts/stop_windows.ps1`**: PowerShell equivalents for Windows.

All scripts should be idempotent — safe to run multiple times.

### Optional Cloud Deployment

The core deployment target is local Docker or any environment that can provide persistent disk at `/app/db`. Managed container platforms with ephemeral filesystems are **not** drop-in compatible with the current SQLite bind-mount persistence model. Cloud deployment is therefore a stretch goal that would require either:

- a persistent attached volume supported by the platform, or
- replacing SQLite with a networked persistent database

---

## 12. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**:
- Market data: simulator generates valid prices, GBM math is correct, Massive API response parsing works, both implementations conform to the abstract interface
- Portfolio: trade execution logic, P&L calculations, edge cases (selling more than owned, buying with insufficient cash, selling at a loss)
- LLM: structured output parsing handles all valid schemas, graceful handling of malformed responses, trade validation within chat flow, and the explicit `allow_trade_execution` approval gate
- API routes: correct status codes, response shapes, error handling

Required negative-path coverage:

- missing `OPENROUTER_API_KEY` falls back to mock mode instead of breaking chat
- invalid ticker input is rejected consistently in manual trade, watchlist, and chat-applied actions
- malformed or out-of-range chat history limits return `400`
- stale quotes reject trade execution
- duplicate watchlist entries return conflict errors
- chat action failures are persisted and rendered without breaking successful sibling actions

**Frontend (React Testing Library or similar)**:
- Component rendering with mock data
- Price flash animation triggers correctly on price changes
- Watchlist CRUD operations
- Portfolio display calculations
- Chat message rendering and loading state

### E2E Tests (in `test/`)

**Infrastructure**: A separate `docker-compose.test.yml` in `test/` that spins up the app container plus a Playwright container. This keeps browser dependencies out of the production image.

**Environment**: Tests run with `LLM_MOCK=true` and `SIMULATOR_SEED` set to a fixed integer, so both the LLM responses and the simulator price paths are deterministic. Assertions may read specific prices safely under a fixed seed; without the seed, tests must only assert structural properties (e.g., price > 0, flashes rendered), not exact values.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell shares: cash increases, position updates or disappears
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send a message, receive a response, and see recommendation, approval-required, or executed states inline as appropriate
- SSE resilience: disconnect and verify reconnection
