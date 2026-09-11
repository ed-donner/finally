# FinAlly — AI Trading Workstation

> **Project Specification.** Section 13 (Design Decisions Log) records choices made after the
> initial draft during a documentation review; where it conflicts with an earlier section, the
> log wins and the section text has been updated to match.

## 1. Vision

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

---

## 2. User Experience

### First Launch

The user runs a single Docker command (or a provided start script). A browser opens to `http://localhost:8000`. No login, no signup. They immediately see:

- A watchlist of 10 default tickers with live-updating prices in a grid
- $10,000 in virtual cash
- A dark, data-rich trading terminal aesthetic
- An AI chat panel ready to assist

### What the User Can Do

- **Watch prices stream** — prices flash green (uptick) or red (downtick) with subtle CSS animations that fade
- **View sparkline mini-charts** — price action beside each ticker in the watchlist, accumulated on the frontend from the SSE stream since page load (sparklines fill in progressively). Price history is **not** persisted server-side in v1: a page reload restarts every sparkline and the detail chart from empty. This is an accepted trade-off (see Section 13).
- **Click a ticker** to see a larger detailed chart in the main chart area (also accumulated from the SSE stream since page load)
- **Buy and sell shares** — market orders only, instant fill at current price, no fees, no confirmation dialog
- **Monitor their portfolio** — a heatmap (treemap) showing positions sized by weight and colored by unrealized P&L, plus a P&L chart tracking total portfolio value over time
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, % change. Percent change is measured against average cost.
- **See realized P&L** — cumulative realized gain/loss from closed and reduced positions, computed from the trade log and shown alongside cash and total value
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

---

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
├── test/                     # Playwright E2E tests (run on host against the compose stack)
├── db/                       # Volume mount target (SQLite file lives here at runtime)
│   └── .gitkeep              # Directory exists in repo; finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── docker-compose.yml        # Single entrypoint — start/stop scripts wrap this
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/db/`** contains schema SQL definitions and seed logic. The backend lazily initializes the database on first request — creating tables and seeding default data if the SQLite file doesn't exist or is empty.
- **`db/`** at the top level is the runtime volume mount point. The SQLite file (`db/finally.db`) is created here by the backend and persists across container restarts via Docker volume.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests, run on the host against the running compose stack. Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
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

> The market data subsystem is already implemented (`backend/app/market/`, see `MARKET_DATA_SUMMARY.md`). The **day-change anchor** and the unknown-ticker fallback below are follow-up additions to that subsystem, not yet built.

### Simulator (Default)

- Generates prices using geometric Brownian motion (GBM) with configurable drift and volatility per ticker
- Updates at ~500ms intervals
- Correlated moves across tickers (e.g., tech stocks move together)
- Occasional random "events" — sudden 2-5% moves on a ticker for drama
- Starts from realistic seed prices (e.g., AAPL ~$190, GOOGL ~$175, etc.)
- Runs as an in-process background task — no external dependencies
- **Unknown tickers**: when a ticker not in the seed table is added, the simulator synthesizes a seed price (a bounded random value in a plausible range) and assigns default GBM drift/volatility with no correlation group. Tickers are validated as 1–5 uppercase letters before being accepted.

### Massive API (Optional)

- REST API polling (not WebSocket) — simpler, works on all tiers
- Polls for the union of all watched tickers on a configurable interval
- Free tier (5 calls/min): poll every 15 seconds
- Paid tiers: poll every 2-15 seconds depending on tier
- Parses REST response into the same format as the simulator
- Uses the API's previous-close value as the day-change anchor (see below); outside market hours prices are the last known close and simply stop changing

### Shared Price Cache

- A single background task (simulator or Massive poller) writes to an in-memory price cache
- The cache holds the latest price, previous price, timestamp, and a **day-change anchor** for each ticker
- The anchor is the previous close (Massive mode) or the first price observed after the ticker started being tracked (simulator mode). "Day change %" everywhere in the UI is `(price − anchor) / anchor`.
- The set of tracked tickers is always `watchlist ∪ tickers with an open position` — a position is never left unpriced, even if its ticker is removed from the watchlist
- SSE streams read from this cache and push updates to connected clients
- Prices are in-memory only. On restart the simulator re-anchors from fresh seed prices while positions/cash persist in SQLite, so unrealized P&L can jump — accepted demo behavior (see Section 13)
- This architecture supports future multi-user scenarios without changes to the data layer

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- Server pushes price updates for all tracked tickers (`watchlist ∪ open positions`) whenever the cache changes, at up to ~500ms cadence. In Massive mode updates are as sparse as the poll interval.
- Each SSE event contains ticker, price, previous price, day-change anchor, timestamp, and change direction (`"up" | "down" | "flat"`)
- The server emits a `: keepalive` comment every ~15s so the client can distinguish an idle stream from a dropped connection and keep the status indicator accurate
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

All timestamps are stored as **UTC ISO-8601 strings with a `Z` suffix** so lexicographic ordering matches chronological ordering.

**`trades` is the source of truth.** `users_profile.cash_balance` and the `positions` table are a cache maintained *in the same transaction* as each trade insert. Cash and positions could be replayed from `trades` alone if the cache is ever lost.

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
- `realized_pnl` REAL (default: `0.0`) — cumulative realized gain/loss for this ticker
- `updated_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`

Cost basis is **weighted average cost**. A buy recomputes `avg_cost`; a sell leaves `avg_cost` unchanged, reduces `quantity`, and adds `(sell_price − avg_cost) × sold_qty` to `realized_pnl`. When `quantity` reaches ~0 the row is **kept** (so `realized_pnl` history survives) with `quantity = 0`; zero-quantity rows are excluded from the heatmap and, by default, the positions table. Total realized P&L is `SUM(realized_pnl)` across rows.

**trades** — Trade history (append-only log)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `side` TEXT (`"buy"` or `"sell"`)
- `quantity` REAL (fractional shares supported)
- `price` REAL
- `executed_at` TEXT (ISO timestamp)

**portfolio_snapshots** — Portfolio value over time (for P&L chart). Recorded every 30 seconds by a background task (only while the market data task is running and the value has changed since the last snapshot), and immediately after each trade execution.
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp)

A background job trims this table to the most recent 7 days on each snapshot write.

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

### Bootstrap
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/bootstrap` | Everything needed for first paint in one round trip: portfolio (positions, cash, realized/unrealized P&L, total value), watchlist with latest prices + anchors, portfolio history, and recent chat history. Individual endpoints below remain for later refreshes. |

### Market Data
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/stream/prices` | SSE stream of live price updates |

### Portfolio
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/portfolio` | Current positions, cash balance, total value, unrealized P&L, cumulative realized P&L |
| POST | `/api/portfolio/trade` | Execute a trade: `{ticker, quantity, side}` |
| GET | `/api/portfolio/history` | Portfolio value snapshots over time (for P&L chart). Optional `?limit=` (default 500, newest N) |

### Watchlist
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Current watchlist tickers with latest prices and day-change anchors |
| POST | `/api/watchlist` | Add a ticker: `{ticker}` |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker (allowed even if a position is held; the ticker stays priced while the position is open) |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat` | Send a message, receive complete JSON response (message + executed actions) |
| GET | `/api/chat/history` | Prior conversation messages (optional `?limit=`, default 50) so a page reload restores the conversation |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check: returns 200 only if the process is up **and** a trivial SQLite query succeeds |

### Trade Validation

`quantity` is always in **shares** (fractional allowed), and must be `> 0`. There are no notional/dollar orders — the AI converts a dollar amount to shares using the live price in its context. Share quantities are rounded to 6 decimals, cash to cents. Buys require sufficient cash; sells require sufficient shares. Each trade executes in a single DB transaction; multiple trades from one chat turn execute sequentially.

### Error Contract

Errors return `{ "error": "<human-readable message>" }` with:
- `400` — validation failure (bad quantity, insufficient cash/shares, malformed ticker)
- `404` — unknown resource (e.g. removing a ticker not on the watchlist)
- `502` — upstream failure (LLM/market data provider)

A trade requested via chat that fails validation is **not** an HTTP error — the failure message is returned inside the chat response so the LLM can relay it.

---

## 9. LLM Integration

When writing code to make calls to LLMs, use the `cerebras` skill to call LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider. Structured Outputs should be used to interpret the results.

There is an OPENROUTER_API_KEY in the .env file in the project root. The app must still start and serve every non-chat route when the key is absent (chat then returns a `502` with a clear message, unless `LLM_MOCK=true`).

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads the last 20 messages of conversation history from the `chat_messages` table
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via LiteLLM → OpenRouter, requesting structured output, using the `cerebras` skill
5. Parses and validates the structured JSON response. If parsing/validation fails, retries once; if it fails again, falls back to returning the raw text as `message` with no actions
6. Auto-executes any trades or watchlist changes specified in the response, collecting per-action success/error results
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
- `trades` (optional): Array of `{ticker, side: "buy"|"sell", quantity}` to auto-execute. Each trade goes through the same validation as manual trades (sufficient cash for buys, sufficient shares for sells)
- `watchlist_changes` (optional): Array of `{ticker, action: "add"|"remove"}`

Note on structured-output support: if the Cerebras provider path does not enforce the JSON schema, the parse-validate-retry-once flow in "How It Works" step 5 is the safety net. Confirm actual behavior via the `cerebras` skill during implementation.

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
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by unrealized P&L % (green = profit, red = loss); zero-quantity rows excluded
- **P&L chart** — line chart showing total portfolio value over time, using data from `portfolio_snapshots`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill.
- **AI chat panel** — docked/collapsible sidebar. Message input, scrolling conversation history (restored from `/api/chat/history` on load), loading indicator while waiting for LLM response. Trade executions and watchlist changes shown inline as confirmations, including failures.
- **Header** — portfolio total value, connection status indicator, cash balance. Total value updates live: recomputed client-side on every SSE tick as `cash + Σ(quantity × latest price)`, not re-fetched.

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`
- **Recharts** for all four visuals (watchlist sparkline, detail chart, P&L line, portfolio treemap) — one dependency covers every case. Revisit only if the detail chart's render performance proves inadequate, in which case swap just that one chart for a canvas library.
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- All API calls go to the same origin (`/api/*`). In production FastAPI serves the static export, so there is no CORS. For local `next dev` on :3000, use `next.config` `rewrites` to proxy `/api/*` to `http://localhost:8000`.
- `output: 'export'` disables Next.js image optimization, route handlers, middleware, and server-side dynamic routes — design within those limits (single route, client-side data fetching)
- Tailwind CSS for styling with a custom dark theme

---

## 11. Docker & Deployment

### Multi-Stage Dockerfile

```
Stage 1: Node 20 slim
  - Copy frontend/
  - npm ci && npm run build (produces static export; npm ci for reproducible builds from the lockfile)

Stage 2: Python 3.12 slim
  - Install uv
  - Copy backend/
  - uv sync (install Python dependencies from lockfile)
  - Copy frontend build output into a static/ directory
  - Expose port 8000
  - CMD: uvicorn serving FastAPI app
```

FastAPI serves the static frontend files and all API routes on port 8000.

### Docker Compose — the single entrypoint

`docker-compose.yml` is the one source of truth for the port mapping, the named volume, and the `.env` file. The SQLite database persists via a named Docker volume mounted at `/app/db`; the backend writes `finally.db` there.

```yaml
# docker-compose.yml (sketch)
services:
  app:
    build: .
    ports: ["8000:8000"]
    env_file: .env
    volumes: ["finally-data:/app/db"]
volumes:
  finally-data:
```

### Start/Stop Scripts

The scripts are thin wrappers so there is no second copy of the run configuration:

- **`scripts/start_mac.sh`** / **`scripts/start_windows.ps1`** — run `docker compose up -d --build`, print the URL, optionally open the browser
- **`scripts/stop_mac.sh`** / **`scripts/stop_windows.ps1`** — run `docker compose down` (the volume is **not** removed, so data persists)

Compose handles "rebuild only if needed", so the scripts carry no build-detection logic. All scripts are idempotent — safe to run multiple times.

### Optional Cloud Deployment

The container is designed to deploy to AWS App Runner, Render, or any container platform. A Terraform configuration for App Runner may be provided in a `deploy/` directory as a stretch goal, but is not part of the core build.

**Security note for any public deployment:** the app has no auth and the portfolio is a single global `user_id="default"`. A public URL means anyone can drive unlimited LLM calls against your `OPENROUTER_API_KEY`. Do not deploy publicly without at least basic auth / a shared secret and request rate limiting.

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

**Infrastructure**: Playwright runs on the host (its deps live in `test/`, never in the Dockerfile, so the production image stays lean). The test runner starts the app with `docker compose up -d` (overriding `LLM_MOCK=true` via env), waits for `/api/health`, runs the specs against `http://localhost:8000`, then `docker compose down`. No dedicated test compose file.

**Environment**: Tests run with `LLM_MOCK=true` by default for speed and determinism.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell part of a position: cash increases, quantity and avg cost behave correctly
- Sell an entire position: row leaves the table/heatmap, realized P&L reflects the gain/loss
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send a message, receive a response, trade execution appears inline; failed trade shows an error inline
- Chat history persists across a page reload
- SSE resilience: disconnect and verify reconnection; status indicator reacts

---

## 13. Design Decisions Log

_Resolved 2026-09-08 during a documentation review. These decisions have been folded into the sections above; this log is the rationale record. The pre-review draft is archived at `planning/archive/PLAN.pre-review-backup-2026-09-08.md`._

### Decisions made

| # | Topic | Decision |
|---|-------|----------|
| 1 | Day-change baseline | Each tracked ticker has a **day-change anchor** in the price cache: previous close (Massive) or first observed price (simulator). "Day change %" = `(price − anchor) / anchor`. Anchor ships in the SSE payload and `/api/watchlist`. (§6) |
| 2 | Price history | **Client-only accumulation** from SSE for v1. A reload restarts every chart from empty. No server-side history buffer, no `/api/history/{ticker}`. (§2) |
| 3 | Unknown tickers | Simulator synthesizes a seed price + default GBM params for any ticker not in the seed table. Tickers validated as 1–5 uppercase letters. (§6) |
| 4 | Watchlist ↔ positions | Tracked set is always `watchlist ∪ open positions`. `DELETE /api/watchlist/{ticker}` is allowed with an open position; the ticker stays priced. (§6, §8) |
| 5 | Realized P&L | Tracked. `positions.realized_pnl` per ticker, accumulated on sells; total shown alongside cash/unrealized. Zero-quantity rows are kept (not deleted) so history survives. (§2, §7) |
| 6 | Accounting method | Weighted average cost. Buy recomputes `avg_cost`; sell leaves it, reduces `quantity`, books realized P&L. % change is vs. `avg_cost`. (§7) |
| 7 | Trade semantics | Shares only (fractional, `> 0`); no notional orders — the LLM converts dollars using its live-price context. Shares rounded to 6 dp, cash to cents. (§8) |
| 8 | Structured outputs | Parse-validate-**retry once**, then fall back to raw text with no actions. Confirm actual Cerebras enforcement via the `cerebras` skill. (§9) |
| 9 | Chat history window | Last **20 messages** into the prompt; `GET /api/chat/history` (default 50) for reload restore. (§8, §9) |
| 10 | Bootstrap endpoint | `GET /api/bootstrap` returns portfolio + watchlist + history + recent chat in one call for first paint. Individual endpoints remain. (§8) |
| 11 | Restart P&L jump | Accepted demo behavior — simulator re-anchors from seed prices on restart while positions persist. Documented, not fixed. (§6) |
| 12 | Snapshot growth | Snapshot only when value changed; trim table to 7 days on each write; `/api/portfolio/history?limit=` (default 500). (§7, §8) |
| 13 | Public-deploy security | Documented warning: no auth + global portfolio + exposed `OPENROUTER_API_KEY` spend. Needs basic auth + rate limiting before any public deploy. (§11) |
| 14 | SSE keepalive | Server emits `: keepalive` every ~15s so the status indicator can tell idle from disconnected. (§6) |
| 15 | Trade atomicity | Each trade = one DB transaction; multiple trades from a chat turn run sequentially. `trades` is the source of truth; `cash_balance`/`positions` are a same-transaction cache. (§7, §8) |
| 16 | Error contract | `{ "error": "..." }` with `400` validation / `404` unknown / `502` upstream. Chat-initiated trade failures ride inside the chat response, not as HTTP errors. (§8) |
| 17 | Dev proxy | `next.config` `rewrites` proxy `/api/*` → `:8000` for `next dev`. (§10) |
| 18 | `output: 'export'` limits | Noted for the frontend agent: no image optimization, route handlers, middleware, or dynamic routes. (§10) |
| 19 | Timestamps | UTC ISO-8601 with `Z` everywhere. (§7) |
| 20 | Charting library | **Recharts** for all four visuals (sparkline, detail, P&L line, treemap). Swap only the detail chart for canvas if perf demands. (§10) |
| 21 | Compose as entrypoint | `docker-compose.yml` is the single source of truth; start/stop scripts are thin wrappers around `docker compose up -d --build` / `down`. (§11) |
| 22 | E2E infra | Playwright runs on the host against the compose stack; no `docker-compose.test.yml`, no Playwright container. (§12) |
| 23 | `npm ci` | Dockerfile stage 1 uses `npm ci`, not `npm install`. (§11) |
| 24 | Editorial | Skill name is `cerebras` (not "cerebras-inference"); `watchlist_changes[].action` ∈ `{add, remove}`; SSE `direction` ∈ `{up, down, flat}`; `/api/health` also runs a trivial DB query; header/separator cleanup. |

### Deferred / not doing (v1)

- **Full event-sourcing** (dropping `positions` / `cash_balance` entirely and replaying `trades` on every request). Kept the cache tables for backend simplicity; `trades` is still the authoritative log if a rebuild is ever needed.
- **Server-side price history** and per-ticker history endpoint — revisit if reload-loses-charts proves annoying in the demo.
- **Trimming `change`/`direction` from the SSE payload** — already built into the market data layer; not worth the churn.
- **Removing unused audit columns** (`users_profile.created_at`) — harmless, left in place.
- **Notional trades** in the trade API — the LLM converts to shares instead.

### Still open (non-blocking)

- **Agent Roles & Build Order.** The plan refers to "the Frontend Engineer agent" and "Backend/Market Data agents" coordinating via `planning/` files, but there's no index of which agent owns which deliverable or the build sequence. Worth a short subsection or a separate `planning/ROLES.md` before the next agent starts.
- **Treemap color scale.** "Colored by unrealized P&L %" needs concrete buckets or a continuous diverging scale + domain clamp — leave to the Frontend Engineer, but flag in review.
