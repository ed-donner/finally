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
| POST | `/api/portfolio/trade` | Execute a trade: `{ticker, quantity, side}` |
| GET | `/api/portfolio/history` | Portfolio value snapshots over time (for P&L chart) |

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

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (for Docker/deployment) |

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

- `message` (required): The conversational text shown to the user
- `trades` (optional): Array of trades to auto-execute. Each trade goes through the same validation as manual trades (sufficient cash for buys, sufficient shares for sells)
- `watchlist_changes` (optional): Array of watchlist modifications

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:
- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

If a trade fails validation (e.g., insufficient cash), the error is included in the chat response so the LLM can inform the user.

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
- **P&L chart** — line chart showing total portfolio value over time, using data from `portfolio_snapshots`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill.
- **AI chat panel** — docked/collapsible sidebar. Message input, scrolling conversation history, loading indicator while waiting for LLM response. Trade executions and watchlist changes shown inline as confirmations.
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`
- Canvas-based charting library preferred (Lightweight Charts or Recharts) for performance
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

The `db/` directory in the project root maps to `/app/db` in the container. The backend writes `finally.db` to this path.

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

## 13. Review — Questions, Clarifications & Simplification Opportunities

_Added 2026-09-08 during a documentation review. Organized by priority. Nothing here blocks starting work, but the items in "Open Questions" should be resolved before the relevant component is built. Market data is already implemented (see `MARKET_DATA_SUMMARY.md`); items touching it are noted as such._

### A. Open Questions (decide before building the affected component)

1. **What is the baseline for "daily change %"?** The watchlist and positions table both show a percent change, but the simulator has no concept of a previous close or session open — it starts from a seed price and drifts. `PriceUpdate.change` is tick-over-tick, not day-over-day. We need to define an anchor price per ticker (e.g. "first price seen this server session" or a stored `open_price`) and expose it (in `/api/watchlist` and the SSE payload, or a separate field). In Massive mode Polygon provides a previous-close value; the simulator needs an equivalent. **Affects: market data (already built — may need a small addition), frontend.**

2. **Is chart/price history purely client-accumulated from SSE?** Section 2 says sparklines accumulate on the frontend since page load. Does the same apply to the main detail chart and the watchlist sparklines after a reload — i.e. all history is lost on refresh? If that's acceptable, state it explicitly. If not, we need a server-side rolling price-history buffer (last N minutes per ticker) and a `GET /api/history/{ticker}` endpoint. Recommendation for simplicity: accept client-only accumulation for v1 and document it.

3. **Adding a ticker that the simulator doesn't know.** The LLM schema example literally adds `PYPL`, which is presumably not in `seed_prices.py`. What happens when an unknown ticker is added in simulator mode? We need a defined fallback (generate a plausible seed price + default GBM drift/volatility, assign to a correlation group or none). Also define ticker validation: format check only, or reject unknown symbols? In Massive mode, what if Polygon returns no data for the symbol? **Affects: market data (already built — confirm behavior), watchlist API.**

4. **Watchlist vs. positions coupling.** If a user removes a ticker from the watchlist while holding a position in it, its price must keep updating (portfolio valuation, P&L, heatmap all depend on it). Confirm the tracked-ticker set is always `watchlist ∪ position tickers`, and that `DELETE /api/watchlist/{ticker}` is allowed (or blocked) when a position exists. Section 6 says SSE pushes "all tickers known to the system" — make explicit that this union is what's tracked.

5. **Realized P&L — tracked or not?** The schema stores `trades` (enough to compute it) but no table or field holds realized P&L, and Section 2/10 only mention *unrealized* P&L. When a position is fully sold, does its row get deleted (Section 12 says "updates or disappears")? If it disappears, realized gains vanish from the UI. Decide: (a) ignore realized P&L for v1, or (b) show a realized P&L figure in the header/portfolio, computed from `trades`.

6. **Position accounting method.** State explicitly that cost basis is **weighted average cost** (not FIFO/LIFO): buys recompute `avg_cost`, sells leave `avg_cost` unchanged and reduce `quantity`. Define what happens at `quantity == 0` (delete row vs. keep with zeroed quantity).

7. **Trade quantity semantics.** `POST /api/portfolio/trade` takes `{ticker, quantity, side}` — quantity is always in *shares*, correct? No notional/dollar orders ("buy $500 of AAPL")? The LLM may naturally want notional trades — should the schema support `{ticker, side, notional}` as an alternative, or is the LLM expected to convert using the price in its context? Also define: minimum quantity, rejection of zero/negative, and rounding precision for fractional shares.

8. **Structured output support on the Cerebras path.** Confirm that `openrouter/openai/gpt-oss-120b` with the Cerebras provider actually enforces JSON-schema structured outputs through OpenRouter (some provider/model combinations only support `json_object` or ignore the schema). If enforcement isn't guaranteed, Section 9 should specify a parse-validate-retry-once strategy and a safe fallback (return the raw message with no actions). The `cerebras` skill should be consulted here.

9. **Conversation history window.** Section 9 step 2 says "recent conversation history" — define the limit (last N messages or a token budget) to keep prompts bounded as `chat_messages` grows.

10. **How does the frontend load prior chat history and initial state on refresh?** There's no `GET /api/chat/history` endpoint, so a page reload shows an empty conversation despite `chat_messages` being persisted. Either add that endpoint or fold chat history into a bootstrap response (see simplification #2).

### B. Gaps & Risks

11. **In-memory prices vs. persisted positions on restart.** Simulator prices reset to seed values on container restart, but `positions.avg_cost` and `cash_balance` persist in SQLite. After a restart, unrealized P&L will visibly jump. Options: persist last prices to the DB on shutdown / periodically, or document this as expected demo behavior.

12. **`portfolio_snapshots` grows unbounded** — 2,880 rows/day at 30s cadence, forever, even when the app is idle. For a long-lived demo/deploy consider: a retention window, downsampling for the chart, or only snapshotting when portfolio value changed materially. `GET /api/portfolio/history` should also take `?from=&to=` or `?limit=` rather than returning the entire series.

13. **Public deployment exposes an auth-free app that spends real money.** Section 11 offers cloud deployment as a stretch goal, but the no-auth design means anyone who finds the URL can drive unlimited LLM calls against your `OPENROUTER_API_KEY`. If cloud deploy is pursued, note the need for at least a shared secret / basic auth / rate limiting, and that the simulated portfolio is globally shared (single `user_id="default"`).

14. **SSE keepalive / connection indicator in Massive mode.** With version-based change detection and 15s Polygon polling, the stream can be silent for long stretches (and overnight/weekends for real data). Confirm the server sends a periodic `: keepalive` comment so the client can distinguish "idle" from "disconnected" and keep the status dot accurate.

15. **Trade execution atomicity.** Manual trades and LLM auto-executed trades both read cash/quantity, validate, then write. Specify that each trade runs in a single transaction (and, if chat can fire multiple trades, that they execute sequentially) so validation can't race.

16. **Error response contract is undefined.** Section 8 lists endpoints but no error shape or status codes. Define a convention (e.g. `{ "error": "message" }` with 400 for validation, 404 for unknown ticker, 502 for LLM/upstream failures) so the frontend can render failures consistently — including the "trade failed validation" case surfaced through chat.

17. **Frontend dev workflow / same-origin.** The single-origin design is clean for production, but `next dev` on :3000 calling :8000 needs a documented proxy (`next.config` rewrites) or the frontend team will hit CORS during development.

18. **`output: 'export'` constraints.** Static export disables Next.js image optimization, route handlers, middleware, and dynamic routes. Worth a one-line note so the frontend agent designs within those limits from the start.

19. **Timestamp format for range queries.** `portfolio_snapshots` / history queries rely on ordering TEXT timestamps — specify **UTC ISO-8601 with a `Z` suffix** everywhere so lexicographic sort == chronological sort.

### C. Simplification Opportunities

20. **Derive cash and positions from the `trades` log (event sourcing).** `cash_balance` is fully determined by `10000 − Σ(buys) + Σ(sells)`, and `positions` by replaying `trades`. Keeping `users_profile.cash_balance` and the `positions` table as separate mutable state introduces update-ordering bugs for zero benefit at this data scale. Consider computing both from `trades` on each request (single user, tiny table). If that feels too radical, at minimum treat `trades` as the source of truth and the others as a cache. This also gives realized P&L for free (#5).

21. **One "bootstrap" endpoint for initial page load.** Instead of the frontend firing `/api/portfolio` + `/api/watchlist` + `/api/portfolio/history` + (missing) chat history on mount, offer `GET /api/bootstrap` returning everything needed for first paint in one round trip. Keeps the individual endpoints for later refreshes if wanted.

22. **Pick one charting library.** Section 10 says "canvas-based preferred (Lightweight Charts **or** Recharts)" — but Recharts is SVG, and Lightweight Charts can't do the portfolio treemap. Choose one for all four visuals (sparkline, detail chart, P&L line, heatmap). Recharts covers line + treemap out of the box and is the simpler single choice unless canvas performance for the detail chart proves insufficient.

23. **Make `docker-compose.yml` the single entrypoint.** Section 11 has both a hand-rolled `docker run` in the start scripts *and* an "optional" compose file — two sources of truth for ports/volumes/env that will drift. Let the scripts just wrap `docker compose up -d --build` / `docker compose down`; the "build image if needed" logic in the scripts disappears because compose handles it.

24. **Drop the Playwright container / `docker-compose.test.yml`.** Running Playwright from the host against the running app container (`npx playwright test` in `test/`) is fewer moving parts and still keeps browser deps out of the production image (they're in `test/`, never in the Dockerfile). A dedicated test compose file is arguably overkill for one app service.

25. **Trim the SSE payload.** `change` and `direction` are both derivable on the client from `price` vs `previous_price`. Sending just `{ticker, price, previous_price, timestamp}` removes server-side logic and shrinks the message. (Minor — skip if `direction` already exists and is convenient.)

26. **Reconsider unused audit columns.** `created_at` on `users_profile` and `updated_at` on `positions` don't appear to drive any feature. Drop them unless a UI element needs them, or keep only if event-sourcing (#20) is rejected.

27. **`npm ci` over `npm install` in the Dockerfile** (Section 11, Stage 1) for reproducible builds from the committed lockfile — a correctness fix as much as a simplification.

### D. Minor / Editorial

28. Enumerate the allowed values for `watchlist_changes[].action` in the Section 9 schema (the example only shows `"add"`; presumably `"remove"` too).
29. Define the SSE `direction` values precisely — `"up" | "down" | "flat"`? (`MARKET_DATA_SUMMARY.md` implies a third state exists.)
30. Specify what `GET /api/health` checks (process only, or also DB connectivity).
31. Section headers: the lone `## Project Specification` under the title is empty; `---` separators are used between sections 3→4 onward but not 1→2→3. Cosmetic consistency pass.
32. Section 9 references the skill as both "cerebras-inference skill" (prose) — the available skill is named `cerebras`. Align the name.
33. Consider adding a short "Agent Roles & Build Order" subsection (or a pointer to a separate doc) — the plan refers to "the Frontend Engineer agent" and "Backend/Market Data agents" and file-based coordination, but there's no index of which agent owns which deliverable or in what sequence.
