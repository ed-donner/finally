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
- **View sparkline mini-charts** — price action beside each ticker in the watchlist. `GET /api/watchlist` returns ~60 points of recent history per ticker so sparklines are populated on first paint; the frontend then extends them live from the SSE stream
- **Click a ticker** to see a larger detailed chart in the main chart area
- **Buy and sell shares** — market orders only, instant fill at current price, no fees, no confirmation dialog
- **Monitor their portfolio** — a heatmap (treemap) showing positions sized by weight and colored by P&L, plus a P&L chart tracking total portfolio value over time
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, % change
- **Chat with the AI assistant** — ask about their portfolio, get analysis, and have the AI execute trades and manage the watchlist through natural language
- **Manage the watchlist** — add/remove tickers manually or via the AI chat

### Visual Design

- **Dark theme**: backgrounds around `#0d1117` or `#1a1a2e`, muted gray borders, no pure black
- **Price flash animations**: brief green/red background highlight on price change, fading over ~500ms via CSS transitions
- **Connection status indicator**: a small colored dot in the header, driven by observable `EventSource` state — green = open and something (price event or heartbeat) received within the last 30s; yellow = `readyState === CONNECTING` after an error, or open but silent for more than 30s; red = `readyState === CLOSED`
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
│   ├── app/
│   │   ├── market/           # Market data subsystem (BUILT)
│   │   ├── db/               # Schema SQL, seed data, lazy init
│   │   └── ...               # Portfolio, watchlist, chat routers
│   ├── tests/                # pytest suite
│   ├── market_data_demo.py   # Rich terminal demo of the price stream
│   └── pyproject.toml
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start_mac.sh          # Launch Docker container (macOS/Linux)
│   ├── stop_mac.sh           # Stop Docker container (macOS/Linux)
│   ├── start_windows.ps1     # Launch Docker container (Windows PowerShell)
│   └── stop_windows.ps1      # Stop Docker container (Windows PowerShell)
├── test/                     # Playwright E2E tests (run on the host)
├── db/                       # Bind mount target (SQLite file lives here at runtime)
│   └── .gitkeep              # Directory exists in repo; finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── docker-compose.yml        # Optional convenience wrapper
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/app/db/`** contains schema SQL definitions and seed logic. The backend lazily initializes the database on first request — creating tables and seeding default data if the SQLite file doesn't exist or is empty.
- **`db/`** at the top level is the runtime volume mount point. The SQLite file (`db/finally.db`) is created here by the backend and persists across container restarts via Docker volume.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests, which run on the host against the running container. Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- **`scripts/`** contains start/stop scripts that wrap Docker commands.

---

## 5. Environment Variables

Copy `.env.example` to `.env` at the project root and fill in what you have. Docker passes the file with `--env-file .env`. For local development outside Docker the backend runs from `backend/` but loads `../.env`, so there is only ever one env file.

```bash
# OpenRouter API key for LLM chat. Everything except /api/chat works without it.
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
- If `OPENROUTER_API_KEY` is absent or empty → the app still starts and every other feature works. `/api/chat` returns a normal-shaped response whose `message` explains that no API key is configured, with empty `trades` and `watchlist_changes`. It never raises, and startup never fails on a missing key.
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
- **Unknown tickers are accepted, not rejected.** `seed_prices.py` only has real parameters for the ten defaults. Any other symbol gets a seed price and volatility synthesized deterministically from a hash of the symbol (so `PYPL` is the same price on every run), and joins the cross-sector correlation group. This keeps the "AI manages your watchlist" demo from dead-ending on a plausible symbol the simulator has never heard of.
- **Ticker validation is one shared rule** used by the manual and LLM paths alike: uppercase the input, then accept `^[A-Z]{1,5}$`. Anything else is rejected with a 400.
- **History backfill.** On startup the simulator generates ~60 points of prior GBM history per ticker (and does the same for each newly added ticker) so sparklines are populated on first paint instead of filling in over the first 30 seconds.

### Massive API (Optional)

- REST API polling (not WebSocket) — simpler, works on all tiers
- Polls for the union of all watched tickers on a configurable interval
- Free tier (5 calls/min): poll every 15 seconds
- Paid tiers: poll every 2-15 seconds depending on tier
- Parses REST response into the same format as the simulator
- **Outside market hours real quotes do not move.** Overnight and at weekends every price is flat, no flash animations fire, and change percentages sit at zero. This is correct behavior, not a bug. The simulator remains the recommended default for demos.

### Shared Price Cache

- A single background task (simulator or Massive poller) writes to an in-memory price cache
- The cache holds the latest price, previous price, and timestamp for each ticker
- SSE streams read from this cache and push updates to connected clients
- This architecture supports future multi-user scenarios without changes to the data layer

### Session Baseline (the "change %" the user sees)

`PriceUpdate.change_percent` compares against the *previous tick* — a number that flickers around zero every 500ms and is useless as a "daily change" column. The cache therefore also records an **open price** per ticker: the first price seen for that ticker after process start (for a ticker added later, the first price after it was added).

- `PriceCache` stores `open_price` alongside latest and previous price
- `PriceUpdate` gains `open_price` and `change_from_open_percent`
- Both appear in the SSE payload; the watchlist "change %" column and the price-flash coloring use `change_from_open_percent`
- The baseline survives page reloads and reconnects, and resets on container restart — acceptable for a simulation

This is an addition to the already-built `backend/app/market/` module and is the one change the market data subsystem still needs.

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- The server polls the price cache every ~500ms and emits **one event containing every tracked ticker**, keyed by symbol — not one event per ticker. It emits only when the cache version has changed, so a quiet market produces no price events.
- A heartbeat comment frame (`: ping\n\n`) is emitted every 15s regardless of price activity. This keeps proxies from dropping an idle connection and lets the frontend distinguish "connected, market quiet" from "backend stalled".
- The stream opens with `retry: 1000`; the client handles reconnection automatically (EventSource has built-in retry)

Payload shape:

```
data: {"AAPL": {"ticker":"AAPL","price":190.50,"previous_price":190.40,
                "open_price":189.20,"timestamp":1753401234.5,
                "change":0.10,"change_percent":0.052,
                "change_from_open_percent":0.687,"direction":"up"}, ...}
```

**Timestamp convention:** SSE timestamps are Unix epoch seconds (float). Every REST timestamp — and every `*_at` column in the database — is an ISO 8601 UTC string. The two formats never mix within a single payload.

---

## 7. Database

### SQLite with Lazy Initialization

The backend checks for the SQLite database on startup (or first request). If the file doesn't exist or tables are missing, it creates the schema and seeds default data. This means:

- No separate migration step
- No manual database setup
- Fresh Docker volumes start with a clean, seeded database automatically

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

**positions** — Current holdings (one row per ticker per user)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `quantity` REAL (fractional shares supported)
- `avg_cost` REAL
- `updated_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`
- A sell that takes quantity to zero **deletes the row** — there are no zero-quantity positions
- Invariant: every ticker with a position is on the watchlist (see section 8)

**trades** — Trade history (append-only log)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `side` TEXT (`"buy"` or `"sell"`)
- `quantity` REAL (fractional shares supported)
- `price` REAL (the actual fill price)
- `executed_at` TEXT (ISO timestamp)
- This is an **audit log only** — no UI panel and no endpoint reads it. It exists so trade history survives, and so realized P&L can be derived later if a panel is ever added.

**portfolio_snapshots** — Portfolio value over time (for P&L chart). Recorded every 30 seconds by a background task, and immediately after each trade execution.
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp)
- One snapshot is written at seed time so the P&L chart has a data point on first launch
- The background task skips the write when total value is unchanged since the last snapshot, which keeps an idle portfolio from accumulating identical rows

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON — trades executed, watchlist changes made; null for user messages)
- `created_at` TEXT (ISO timestamp)

### What Is Not Tracked

Realized P&L has no column and no display. On a sell, the proceeds land in `cash_balance` and the position's cost basis goes away with it. Total portfolio value stays correct and the positions table shows *unrealized* P&L only. This is a deliberate simplification — the `trades` table holds everything needed to compute realized P&L should it ever be wanted.

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
| POST | `/api/portfolio/trade` | Execute a trade: `{ticker, quantity, side}` |
| GET | `/api/portfolio/history` | Portfolio value snapshots over time (for P&L chart). Query params: `?limit=` (default 500, newest first) and `?since=` (ISO timestamp) |

### Watchlist
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Watchlist tickers with latest price and ~60 points of recent history for sparklines |
| POST | `/api/watchlist` | Add a ticker: `{ticker}` |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker. Rejected with 409 if a position is held in it |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/chat` | Conversation history, oldest first, so the panel repopulates after a reload. Query param: `?limit=` (default 100) |
| POST | `/api/chat` | Send a message, receive complete JSON response (message + executed actions) |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check. Returns `{status, market_source, tickers_cached, newest_price_age_seconds}` — enough to answer "is the stream alive?" in one request |

### Request and Response Shapes

```jsonc
// GET /api/portfolio
{
  "cash_balance": 8234.50,
  "total_value": 10120.75,
  "positions": [
    {"ticker": "AAPL", "quantity": 10, "avg_cost": 188.60,
     "current_price": 190.50, "market_value": 1905.00,
     "unrealized_pnl": 19.00, "unrealized_pnl_percent": 1.007}
  ]
}

// POST /api/portfolio/trade  ->  {"ticker": "AAPL", "quantity": 10, "side": "buy"}
{
  "ticker": "AAPL", "side": "buy", "quantity": 10,
  "fill_price": 190.52,          // the server-side price, not what the client saw
  "total_cost": 1905.20,
  "cash_balance": 8234.50,
  "executed_at": "2026-07-25T14:03:11Z"
}

// GET /api/watchlist
{
  "tickers": [
    {"ticker": "AAPL", "price": 190.50, "open_price": 189.20,
     "change_from_open_percent": 0.687,
     "history": [189.20, 189.35, ...]}   // ~60 points, oldest first
  ]
}

// GET /api/portfolio/history
{"snapshots": [{"total_value": 10000.0, "recorded_at": "2026-07-25T14:00:00Z"}]}

// GET /api/chat
{"messages": [{"role": "user", "content": "...", "actions": null,
               "created_at": "2026-07-25T14:02:00Z"}]}
```

Errors use FastAPI's default envelope — `{"detail": "Insufficient cash: need $1905.20, have $800.00"}` — with 400 for validation and business-rule failures, 404 for unknown tickers on DELETE, and 409 for removing a watchlist entry with an open position. Messages are written to be shown to the user verbatim.

### Trade Rules

- **Market orders only, no shorting, no margin.** Buys require sufficient cash; sells require sufficient shares. There is no borrowing on either side.
- **Fill price is the server's price.** The client's displayed price is advisory; the server fills at whatever is in the price cache when the request lands and returns that as `fill_price`. The UI shows the fill it got, not the price that was clicked.
- **A just-added ticker may not have a price yet.** Rather than failing, the endpoint polls the cache for up to 2 seconds (every 200ms) waiting for a first tick. The simulator publishes within ~500ms, so this effectively never expires; if it does, return 400 with a "no price available yet, try again" message.
- **Quantity** must be a finite number greater than zero, rounded to at most 4 decimal places. Zero, negative, `NaN`, and `Infinity` are 400s.
- **Trading a ticker that is not on the watchlist adds it to the watchlist** as part of the trade. This holds the invariant that every position has a live price feed, and it is what makes "forbid removing a ticker you hold" sufficient on its own.
- **Every trade writes a `portfolio_snapshots` row immediately**, so the P&L chart shows the step.

---

## 9. LLM Integration

When writing code to make calls to LLMs, use cerebras-inference skill to use LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider. Structured Outputs should be used to interpret the results.

There is an OPENROUTER_API_KEY in the .env file in the project root.

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads recent conversation history from the `chat_messages` table
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via LiteLLM → OpenRouter, requesting structured output, using the cerebras-inference skill
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

**All three fields are required**, with empty arrays when there is nothing to do. No optional keys — structured outputs are more reliable without them, and the parsing code loses its `None` branches. A response with no actions is `{"message": "...", "trades": [], "watchlist_changes": []}`.

- `message`: The conversational text shown to the user
- `trades`: Trades to auto-execute. Each goes through exactly the same validation as a manual trade — same fill-price rule, same quantity rules, same auto-add-to-watchlist behavior
- `watchlist_changes`: Watchlist modifications, subject to the same `^[A-Z]{1,5}$` rule and the same "cannot remove a held ticker" rule

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:
- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

If a trade fails validation (e.g., insufficient cash), the error is included in the chat response so the LLM can inform the user.

### Keeping the UI in Sync

The LLM changes server state the user did not directly cause. Rather than push those changes down a second channel, the frontend refetches `/api/portfolio` and `/api/watchlist` after any chat response whose `trades` or `watchlist_changes` are non-empty — and after any manual trade. That one rule covers every path by which portfolio state changes.

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

The mock is keyword-triggered on the lowercased user message, checked in this order. The E2E suite asserts against this contract, so it is part of the spec, not an implementation detail:

| Message contains | Mock response |
|---|---|
| `"buy"` | `trades: [{ticker, side: "buy", quantity: 1}]` — ticker is the first `^[A-Z]{1,5}$` token in the message, else `AAPL` |
| `"sell"` | Same, with `side: "sell"` |
| `"watch"` or `"add"` | `watchlist_changes: [{ticker, action: "add"}]`, same ticker extraction, defaulting to `PYPL` |
| `"remove"` | `watchlist_changes: [{ticker, action: "remove"}]` |
| anything else | A fixed analysis string that echoes live portfolio numbers — `"You are holding N positions worth $X with $Y in cash."` — and empty arrays |

The mock still routes its trades and watchlist changes through the real execution and validation path, so an E2E test that mocks the LLM is still exercising genuine trade logic.

---

## 10. Frontend Design

### Layout

The frontend is a single-page application with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), change % since the session open (`change_from_open_percent`, not the tick-over-tick number), and a sparkline mini-chart seeded from the watchlist response and extended from SSE
- **Main chart area** — larger chart for the currently selected ticker, with at minimum price over time. Clicking a ticker in the watchlist selects it here.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss)
- **P&L chart** — line chart showing total portfolio value over time, using data from `portfolio_snapshots`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill.
- **AI chat panel** — docked/collapsible sidebar. Message input, scrolling conversation history, loading indicator while waiting for LLM response. Trade executions and watchlist changes shown inline as confirmations.
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Live Values Are Computed on the Client

The only live channel is the price stream. There is no portfolio SSE channel and no polling loop. The client holds `cash_balance` and positions from `/api/portfolio` and recomputes `cash + Σ(quantity × live price)` on every SSE frame. The same derivation drives the header total, the positions table's current-price and P&L columns, the heatmap colors, and the live end of the P&L line. Server state is refetched only on the events listed in section 9.

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`
- **Recharts for every chart** — the line chart, the sparklines, and the treemap. Lightweight Charts has no treemap, so choosing it would mean shipping a second charting library and a second bundle for one panel. One library, one visual language.
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed
- Tailwind CSS for styling with a custom dark theme

---

## 11. Docker & Deployment

### Multi-Stage Dockerfile

```
Stage 1: Node 22 slim
  - Copy frontend/
  - npm ci && npm run build (produces static export)

Stage 2: Python 3.12 slim
  - Install uv
  - Copy backend/
  - uv sync --frozen --no-dev (install Python dependencies from lockfile)
  - Copy frontend build output into a static/ directory
  - Expose port 8000
  - CMD: uvicorn serving FastAPI app
```

`npm ci` and `uv sync --frozen` build from the lockfiles rather than re-resolving, which is the reason for having lockfiles.

FastAPI serves the static frontend files and all API routes on port 8000.

**Mount order matters.** `app.mount("/", StaticFiles(directory="static", html=True))` must come *after* every `/api/*` router is registered. Mounted first, it shadows the API and every endpoint 404s while the UI appears to work — the most common way this architecture breaks.

### Docker Volume

The SQLite database persists via a bind mount to the `db/` directory in the project root:

```bash
docker run -v "$PWD/db:/app/db" -p 8000:8000 --env-file .env finally
```

The backend writes `finally.db` to this path. A bind mount rather than a named volume is deliberate for a teaching project: students can see the database file, inspect it, and delete it to reset. The start scripts use the platform-appropriate form of `$PWD`.

### Start/Stop Scripts

**`scripts/start_mac.sh`** (macOS/Linux):
- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container with the volume mount, port mapping, and `.env` file
- Prints the URL to access the app
- Optionally opens the browser

**`scripts/stop_mac.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT remove the volume (data persists)

**`scripts/start_windows.ps1`** / **`scripts/stop_windows.ps1`**: PowerShell equivalents for Windows.

All scripts should be idempotent — safe to run multiple times.

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

**Infrastructure**: Playwright runs **on the host** against the container started by the normal start script — `npx playwright test` pointed at `http://localhost:8000`. No test compose file, no second container, no service graph, no networking hop. Browser dependencies stay out of the production image because they were never in it. This is materially simpler for students, particularly on Windows.

**Environment**: Tests run with `LLM_MOCK=true` by default for speed and determinism, and assert against the mock contract in section 9.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming, sparklines are already populated
- Add and remove a ticker from the watchlist
- Removing a ticker with an open position is rejected with a visible error
- Buy shares: cash decreases, position appears, portfolio updates, fill price is displayed
- Sell shares: cash increases, position updates or disappears entirely when it hits zero
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send a message, receive a response, trade execution appears inline; reload the page and the conversation is still there
- SSE resilience: block the `/api/stream/prices` route with `page.route()`, assert the status dot leaves green, unblock, assert it returns to green

---

## 13. Build Order

The market data subsystem (`backend/app/market/`) is built and tested. Everything else is open. This order is what an agent may assume already exists when it starts.

1. **Session baseline in the market module** — `open_price` and `change_from_open_percent` in `PriceCache` and `PriceUpdate`, history backfill, synthesized params for unknown tickers, SSE heartbeat. The one remaining change to an otherwise finished module. Do it first: the frontend contract depends on it.
2. **Database and portfolio API** — schema, lazy init, seed data, `/api/portfolio`, `/api/portfolio/trade`, `/api/portfolio/history`, the 30-second snapshot task. Reads the price cache for valuation.
3. **Watchlist API** — `/api/watchlist` CRUD, wired to `add_ticker` / `remove_ticker` on the market source.
4. **Frontend shell** — layout, SSE wiring, watchlist panel, header, trade bar.
5. **Charts** — main chart, sparklines, heatmap, P&L chart. Recharts throughout.
6. **Chat** — `/api/chat` GET and POST, mock mode, LLM integration, chat panel.
7. **Docker and start/stop scripts.**
8. **E2E tests.**

Steps 1-3 are independent of each other and can run in parallel. Step 4 depends on 1 and 3. Everything from step 4 onward builds against the shapes in section 8, not against the backend implementation.

---

## 14. Review Decisions

A documentation review raised 24 issues against this plan. All are resolved in the sections above; this log records what was decided and where it now lives, so the reasoning survives.

| # | Decision | Section |
|---|---|---|
| 1 | "Daily change %" is measured against a session open price recorded in `PriceCache`, not the previous tick | 6 |
| 2 | Unknown tickers are accepted; seed price and volatility are synthesized deterministically from the symbol | 6 |
| 3 | A watchlist ticker with an open position cannot be removed (409). Trades auto-add their ticker, so every position always has a feed | 7, 8 |
| 4 | Trades fill at the server-side price and return it. A ticker with no price yet is waited on for up to 2s rather than rejected | 8 |
| 5 | `GET /api/chat` added so the conversation survives a page reload | 8 |
| 6 | `LLM_MOCK` is keyword-triggered and specified as a contract the E2E suite asserts against | 9 |
| 7 | Bind mount `./db:/app/db`; the contradictory named-volume line is gone | 11 |
| 8 | SSE contract corrected to what is actually implemented: one event carrying all tickers, emitted only on change | 6 |
| 9 | Timestamps are epoch seconds on SSE and ISO 8601 UTC everywhere else | 6 |
| 10 | `OPENROUTER_API_KEY` is optional; chat degrades to a friendly message, the app does not fail | 5 |
| 11 | Request and response shapes written into section 8 directly, rather than a separate contract document — one document, one authority | 8 |
| 12 | Build order stated | 13 |
| 13 | Trade validation fully specified: no shorting or margin, quantity > 0 at 4dp, zero-quantity positions deleted | 7, 8 |
| 14 | Realized P&L is deliberately not tracked; derivable from `trades` if ever wanted | 7 |
| 15 | `trades` is an audit log with no reader and no UI | 7 |
| 16 | SSE heartbeat every 15s; connection dot defined in terms of observable `EventSource` state | 2, 6 |
| 17 | Sparklines seeded from `/api/watchlist`; a snapshot is written at seed time so no panel is empty on first paint | 2, 6, 7 |
| 18 | `/api/portfolio/history` takes `?limit=` and `?since=`; unchanged snapshots are skipped | 7, 8 |
| 19 | Massive off-hours flatness documented as expected behavior | 6 |
| 20 | One refetch rule covers every server-side state change the user did not directly cause | 9 |
| 21 | Live totals are computed on the client from cash, positions, and the SSE stream — no portfolio channel, no polling | 10 |
| 22 | Recharts for every chart, including the treemap | 10 |
| 23 | Playwright runs on the host against the container; no test compose file | 4, 12 |
| 24 | All three LLM output fields are required, with empty arrays as the default | 9 |

Minor notes also applied: static mount ordering (11), `npm ci` and `uv sync --frozen --no-dev` (11), Node 22 (11), `.env.example` instructions (5), a `/api/health` payload worth reading (8), the SSE resilience test rewritten as route blocking (12), and the directory tree refreshed (4).
