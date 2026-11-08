# FinAlly — AI Trading Workstation

## 1. Vision

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

## 2. User Experience

### First Launch

The user runs a single Docker command (or a provided start script) and opens `http://localhost:8000` (the start scripts can open the browser via an optional flag). No login, no signup. They immediately see:

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
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, % change (relative to average cost)
- **Chat with the AI assistant** — ask about their portfolio, get analysis, and have the AI execute trades and manage the watchlist through natural language
- **Manage the watchlist** — add/remove tickers manually or via the AI chat
- **Reset the portfolio** — one action returns the account to $10,000 cash, clears positions, trades, snapshots and chat history, and restores the 10 default watchlist tickers (useful for demos and E2E tests)

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
- **AI integration**: Anthropic API (official Python SDK, configured via `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL` in `.env`), with structured outputs for trade execution
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
│   └── db/                   # Schema definitions and seed data
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start_mac.sh          # Thin wrapper: docker build/run (macOS/Linux)
│   ├── stop_mac.sh           # Thin wrapper: docker stop/rm (macOS/Linux)
│   ├── start_windows.ps1     # Thin wrapper: docker build/run (Windows PowerShell)
│   └── stop_windows.ps1      # Thin wrapper: docker stop/rm (Windows PowerShell)
├── test/                     # Playwright E2E tests (run from the host) + docker-compose.test.yml (app container only)
├── db/                       # Local non-Docker runs only (SQLite file lives here); Docker uses a named volume
│   └── .gitkeep              # Directory exists in repo; finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/db/`** contains schema SQL definitions and seed logic. The backend initializes the database at startup, in the FastAPI lifespan handler — creating tables and seeding default data if the SQLite file doesn't exist or is empty.
- **`db/`** at the top level is where the SQLite file (`db/finally.db`) lives for local non-Docker runs. In Docker, `/app/db` is a named volume (`finally-data`), so data persists across container restarts.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests, which run from the host against the app container, and `docker-compose.test.yml`, which starts that container with `LLM_MOCK=true`. Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- **`scripts/`** contains start/stop scripts that wrap Docker commands.

---

## 5. Environment Variables

```bash
# Required for chat unless LLM_MOCK=true: Anthropic API access for LLM chat functionality
ANTHROPIC_AUTH_TOKEN=your-token-here
ANTHROPIC_BASE_URL=https://your-anthropic-compatible-endpoint
ANTHROPIC_MODEL=your-model-name

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
- In Docker, the start scripts pass env vars with `--env-file`; the `.env` file is not inside the container. The backend reads `.env` from the project root only for local runs
- If the `ANTHROPIC_*` variables are missing (and `LLM_MOCK` is not `true`), the app still starts; only `/api/chat` fails, and the chat panel shows a clear error saying the token is missing

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
- Accepts any valid ticker: a ticker without a known seed price gets a random seed price and default drift/volatility
- Runs as an in-process background task — no external dependencies

### Massive API (Optional)

- REST API polling (not WebSocket) — simpler, works on all tiers
- Polls for the union of watchlist and open-position tickers on a configurable interval, using the batch snapshot endpoint (`/v2/snapshot/locale/us/markets/stocks/tickers`) so each poll is a single call. To verify: confirm the exact path against the Massive docs before implementing
- Free tier (5 calls/min): poll every 15 seconds
- Paid tiers: poll every 2-15 seconds depending on tier
- Parses REST response into the same format as the simulator
- Validates a newly added or traded ticker that is not yet in the cache with one single-ticker snapshot call; a ticker with no data is rejected. These calls count against the rate limit: on the free tier, polling uses 4 of the 5 calls/min, leaving 1 spare for validation
- Prices are stale outside market hours; this is acceptable. The UI shows a small "market closed" label only when Massive data is stale (the simulator runs 24/7)

### Shared Price Cache

- A single background task (simulator or Massive poller) writes to an in-memory price cache
- The cache covers the union of watchlist tickers and open positions, so a position in a ticker removed from the watchlist keeps its price
- The cache holds the latest price, previous price, timestamp, and baseline for each ticker. The baseline for daily change % is the seed price (simulator) or the previous close (Massive)
- SSE streams read from this cache and push updates to connected clients
- This architecture supports future multi-user scenarios without changes to the data layer

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- Server pushes price updates for all tickers in the price cache (watchlist plus open positions) at a regular cadence (~500ms)
- Each tick is one batched JSON event holding every ticker's price, previous price, timestamp, change direction, and daily change %
- Client handles reconnection automatically (EventSource has built-in retry)

---

## 7. Database

### SQLite with Startup Initialization

The backend checks for the SQLite database in the FastAPI lifespan handler at startup. If the file doesn't exist or tables are missing, it creates the schema and seeds default data. This means:

- No separate migration step
- No manual database setup
- Fresh Docker volumes start with a clean, seeded database automatically

### Schema

All tables include a `user_id` column defaulting to `"default"`. This is hardcoded for now (single-user) but enables future multi-user support without schema migration. Code reads it through a single `DEFAULT_USER` constant.

All writes (trades, 30-second snapshots, chat) go through a single write lock, and each trade (cash update, position upsert, trade insert) runs in one transaction.

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

**portfolio_snapshots** — Portfolio value over time (for P&L chart). Recorded every 30 seconds by a background task, and immediately after each trade execution.
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp)

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON, null for user messages): `{"trades": [{ticker, side, quantity, status, error}], "watchlist_changes": [{ticker, action, status, error}]}`, where `status` is `executed` or `failed` and `error` holds the failure text
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
| GET | `/api/portfolio` | Cash balance, total value, unrealized P&L, and the positions list |
| POST | `/api/portfolio/trade` | Execute a trade: `{ticker, quantity, side}` |
| GET | `/api/portfolio/history` | Portfolio value snapshots over time (for P&L chart), at most the last 24 hours |
| POST | `/api/portfolio/reset` | Reset to $10,000 cash; clears positions, trades, snapshots and chat history; restores the 10 default watchlist tickers |

Total value is `cash + sum(quantity * current_price)`. The P&L chart baseline is the starting $10,000.

### Watchlist
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Current watchlist tickers with latest prices |
| POST | `/api/watchlist` | Add a ticker: `{ticker}` |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat` | Send a message, receive complete JSON response (message + executed actions) |
| GET | `/api/chat` | Stored conversation history, so the UI can show prior messages after a refresh: a list of `{id, role, content, actions, created_at}` |

`POST /api/chat` takes `{message}` and returns `{message, actions}`, where `actions` has the same shape as `chat_messages.actions` in section 7: `{trades: [...], watchlist_changes: [...]}`.

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (for Docker/deployment) |

### Trade Rules

- Tickers are uppercased and trimmed at the API boundary: 1-5 characters from A-Z, `.` and `-`
- Quantity must be greater than 0 and is rounded to 4 decimals
- Buys update `avg_cost` as a weighted average; a full sell deletes the position row
- No short selling
- Any ticker that has a price may be traded, not only watchlist tickers
- Removing a watchlist ticker that has a position is allowed: the position stays and its price keeps streaming
- Manual API calls and LLM-driven trades and watchlist changes share one service function each

### Error Contract

Errors use the body `{"error": "<code>", "detail": "..."}`, for example `{"error": "insufficient_cash", "detail": "..."}`. Validation errors (including a malformed ticker) return 400. Unknown tickers return 404: this only happens with Massive, when a ticker has no quote. The simulator accepts any well-formed ticker. The chat flow reports each trade's error individually.

---

## 9. LLM Integration

When writing code to make calls to LLMs, use the official Anthropic Python SDK (`anthropic`), configured entirely from the `.env` file in the project root: `ANTHROPIC_AUTH_TOKEN` (auth token), `ANTHROPIC_BASE_URL` (endpoint) and `ANTHROPIC_MODEL` (model name). The SDK reads the token and base URL from the environment on its own; the backend loads `ANTHROPIC_MODEL` and passes it as the `model` argument on every call. Never hardcode the model, endpoint or token. Structured Outputs should be used to interpret the results.

Structured-output support on the configured gateway and model is unverified. There is no standalone smoke test; the first `/api/chat` increment tests it. Fallback order: native structured output, then a forced tool call, then parsing JSON from text with one retry.

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads the last 20 messages of conversation history from the `chat_messages` table
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via the Anthropic API, requesting structured output
5. Parses the complete structured JSON response
6. Auto-executes any trades or watchlist changes specified in the response
7. Stores the message and executed actions in `chat_messages`
8. Returns the complete JSON response to the frontend (no token-by-token streaming — a loading indicator is sufficient)

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
- `trades` (optional): Array of trades to auto-execute. Each trade goes through the same validation as manual trades (sufficient cash for buys, sufficient shares for sells). The schema has no price: trades always fill at the server's current cache price, whatever price the LLM quoted
- `watchlist_changes` (optional): Array of watchlist modifications; `action` is `"add"` or `"remove"`. Adding a ticker already on the watchlist, or removing one that is not, is reported as `failed` with a clear error

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:
- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

If a trade fails validation (e.g., insufficient cash), the error is included in the chat response so the LLM can inform the user. Executed and failed actions are stored in `chat_messages.actions` and shown inline in the chat panel.

### System Prompt Guidance

The LLM should be prompted as "FinAlly, an AI trading assistant" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven in responses
- Always respond with valid structured JSON

### LLM Mock Mode

When `LLM_MOCK=true`, the backend returns deterministic mock responses instead of calling the Anthropic API:
Matching is case-insensitive and checked in this order:
- A message containing "buy" returns a buy of 1 AAPL (this wins even if the message also contains "add")
- Otherwise, a message containing "add X" returns a watchlist add of X, where X is the word after "add", uppercased
- Anything else returns a fixed text reply

This enables:
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
- **P&L chart** — line chart showing total portfolio value over time, using data from `portfolio_snapshots`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change (relative to avg cost)
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill.
- **AI chat panel** — docked/collapsible sidebar. Message input, scrolling conversation history, loading indicator while waiting for LLM response. Trade executions and watchlist changes shown inline as confirmations.
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`
- Use Recharts for all charts (treemap, line charts, sparklines); it is the only charting dependency. Revisit only if 10 sparklines at 2 Hz prove too slow
- Local dev: a Next.js rewrite proxies `/api` to `localhost:8000`; document the setup in the README
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed
- Tailwind CSS for styling with a custom dark theme

---

## 11. Docker & Deployment

### Multi-Stage Dockerfile

```
Stage 1: Node 22 slim (current LTS; Node 20 is end of life)
  - Copy frontend/
  - npm install && npm run build (produces static export)

Stage 2: Python 3.12 slim
  - Install uv
  - Copy backend/
  - uv sync (install Python dependencies from lockfile)
  - Copy frontend build output into a static/ directory
  - Expose port 8000
  - CMD: uvicorn serving FastAPI app with exactly one worker
```

FastAPI serves the static frontend files and all API routes on port 8000. Unknown non-`/api` paths fall back to `index.html`. The app must run as a single process because the price cache and background tasks live in memory.

Node 22 and Python 3.12 are pinned, and both `uv.lock` and `package-lock.json` are committed.

### Docker Volume

The SQLite database persists via a named Docker volume:

```bash
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally
```

The backend writes `finally.db` to `/app/db` in the container. The top-level `db/` directory is only used for local non-Docker runs.

### Start/Stop Scripts

**`scripts/start_mac.sh`** (macOS/Linux):
- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container with the named volume, port mapping, and `--env-file .env`
- Prints the URL to access the app
- Opens the browser only when an optional flag is passed

**`scripts/stop_mac.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT remove the volume (data persists)

**`scripts/start_windows.ps1`** / **`scripts/stop_windows.ps1`**: PowerShell equivalents for Windows, with the same `--build` and optional browser-open flags.

All scripts are thin wrappers around `docker build/run/stop/rm` and should be idempotent — safe to run multiple times. There is no root `docker-compose.yml`.

---

## 12. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**:
- Market data: simulator generates valid prices, GBM math is correct, Massive API response parsing works, both implementations conform to the abstract interface
- Portfolio: trade execution logic, P&L calculations, edge cases (selling more than owned, buying with insufficient cash, selling at a loss)
- LLM: structured output parsing handles all valid schemas, graceful handling of malformed responses, trade validation within chat flow
- API routes: correct status codes, response shapes, error handling

**Frontend (React Testing Library or similar)** — a minimal set; E2E carries the rest:
- Price flash animation triggers correctly on price changes
- Portfolio display calculations

### E2E Tests (in `test/`)

**Infrastructure**: A `docker-compose.test.yml` in `test/` that starts the app container. Playwright runs from the host against it, which keeps browser dependencies out of the production image and avoids a second image.

**Environment**: Tests run with `LLM_MOCK=true` by default for speed and determinism.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell shares: cash increases, position updates or disappears
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send a message, receive a response, trade execution appears inline
- Remove a watchlist ticker that holds a position: the position stays and its price keeps streaming
- Reset portfolio: cash returns to $10,000, positions clear, default watchlist is restored
- Missing token: with `LLM_MOCK=false` and no `ANTHROPIC_*` variables, the chat panel shows a clear error and the rest of the app works
- SSE resilience: disconnect and verify reconnection
