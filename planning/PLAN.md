# FinAlly — AI Trading Workstation

## Project Specification

## 1. Vision

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

## 2. User Experience

### First Launch

The user runs a single command — `docker compose up` (§11). A browser opens to `http://localhost:8000`. No login, no signup. They immediately see:

- A watchlist of 10 default tickers with live-updating prices in a grid
- $10,000 in virtual cash
- A dark, data-rich trading terminal aesthetic
- An AI chat panel ready to assist

### What the User Can Do

- **Watch prices stream** — prices flash green (uptick) or red (downtick) with subtle CSS animations that fade
- **View sparkline mini-charts** — price action beside each ticker in the watchlist, accumulated on the frontend from the SSE stream since page load (sparklines fill in progressively)
- **Click a ticker** to see a larger detailed chart in the main chart area — like the sparklines, this chart accumulates from the SSE stream since page load, so it starts near-empty and fills in progressively. There is no historical price backfill, and a page reload starts it over.
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
- **Responsive but desktop-first**: optimized for wide screens. Two breakpoints, with an explicit collapse order so the layout degrades predictably:
  - **≥1280px (target)** — full layout, all panels visible, chat docked as a sidebar
  - **1024–1279px** — chat panel becomes a collapsible overlay triggered by a header button
  - **<1024px** — portfolio heatmap hides (a treemap below this width is unreadable); watchlist and positions table stack vertically. Below 768px is not supported.

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
- **Database**: SQLite, single file at `db/finally.db`, bind-mounted from the project directory for persistence
- **Real-time data**: Server-Sent Events (SSE) — simpler than WebSockets, one-way server→client push, works everywhere
- **AI integration**: LiteLLM → OpenRouter (Cerebras for fast inference), with structured outputs for trade execution
- **Market data**: Environment-variable driven — simulator by default, real data via Massive API if key provided

### Why These Choices

| Decision | Rationale |
|---|---|
| SSE over WebSockets | One-way push is all we need; simpler, no bidirectional complexity, universal browser support |
| Static Next.js export | Single origin, no CORS issues, one port, one container, simple deployment |
| SQLite over Postgres | No auth = no multi-user = no need for a database server; self-contained, zero config |
| Single Docker container | Students run one command; one service, no orchestration between services |
| Compose as the only entry point | `docker compose up` is one identical command on macOS, Linux, and Windows. Volume, port, and env-file config live declaratively in one file instead of four hand-maintained platform scripts |
| uv for Python | Fast, modern Python project management; reproducible lockfile; what students should learn |
| Market orders only | Eliminates order book, limit order logic, partial fills — dramatically simpler portfolio math |

---

## 4. Directory Structure

```
finally/
├── frontend/                 # Next.js TypeScript project (static export)
├── backend/                  # FastAPI uv project (Python)
│   └── app/
│       ├── market/           # Market data subsystem (complete — see MARKET_DATA_SUMMARY.md)
│       └── db.py             # Schema, seed data, connection handling (single module)
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── test/                     # Playwright E2E tests + docker-compose.test.yml
├── db/                       # Bind mount target (SQLite file lives here at runtime)
│   └── .gitkeep              # Directory exists in repo; finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── docker-compose.yml        # The way the app is started (see §11)
├── .env                      # Environment variables (gitignored)
├── .env.example              # Committed template — copy to .env and fill in
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/app/db.py`** contains the schema, seed logic, and connection handling in one module. It must live *inside* the `app` package: `backend/pyproject.toml` declares `packages = ["app"]` for the wheel build, so anything outside `app/` is not shipped. The backend creates tables and seeds default data at startup (see §7) if the SQLite file doesn't exist or is empty.
- **`db/`** at the top level is the runtime bind mount point. The SQLite file (`db/finally.db`) is created here by the backend and is directly visible and deletable from the project directory — students can inspect it with any SQLite tool, and `rm db/finally.db` is the documented reset path (§8).
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests and supporting infrastructure (e.g., `docker-compose.test.yml`). Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.

> **Naming note:** there is exactly one directory named `db/` (the runtime mount at the project root). Database *code* lives in `backend/app/db.py`. Do not create a `backend/db/` directory.

---

## 5. Environment Variables

Both keys are **optional**. Every key follows the same rule: *absent → use the built-in fake.* The app always starts and is always fully usable; supplying a key upgrades a subsystem from simulated to real.

```bash
# Optional: OpenRouter API key for LLM chat.
# If not set, chat runs in mock mode (deterministic canned responses).
OPENROUTER_API_KEY=

# Optional: Massive (Polygon.io) API key for real market data.
# If not set, the built-in market simulator is used (recommended for most users).
MASSIVE_API_KEY=

# Optional override: force mock LLM responses even when a key IS set (for tests/CI).
LLM_MOCK=false
```

`.env.example` is a committed copy of the block above; `.env` is gitignored. First-run setup is `cp .env.example .env`.

### Behavior

| Condition | Result |
|---|---|
| `MASSIVE_API_KEY` set and non-empty | Massive REST API for market data |
| `MASSIVE_API_KEY` absent or empty | Built-in market simulator |
| `OPENROUTER_API_KEY` absent or empty | Chat runs in **mock mode** — no network calls, deterministic responses |
| `OPENROUTER_API_KEY` set and non-empty | Chat calls OpenRouter |
| `LLM_MOCK=true` | Mock mode, regardless of whether a key is present |

The backend reads `.env` from the project root (supplied to the container via compose's `env_file`).

**Rationale:** mock mode is reached by *absence of a key*, exactly as the simulator is. `LLM_MOCK` exists only for the narrow case of a developer who has a working key but wants deterministic, free, offline test runs — it is an override, not the primary switch. The app never refuses to start over a missing key.

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

**Tickers outside the default ten.** The user and the LLM can both add arbitrary tickers to the watchlist, so the simulator must price symbols it has no seed data for. This is already implemented in `app/market/simulator.py` and `seed_prices.py`:

- **Seed price** — `SEED_PRICES` if known, otherwise a random price in `$50–$300`
- **GBM params** — `TICKER_PARAMS` if known, otherwise `DEFAULT_PARAMS` (σ=0.25, μ=0.05)
- **Correlation** — sector group if known, otherwise `CROSS_GROUP_CORR` (0.3) against everything

Simulated prices for non-seeded tickers are therefore *arbitrary* — `PYPL` may open at $63 or $291. This is acceptable and expected in simulator mode; it is worth surfacing in the UI copy (e.g. a "simulated" badge) so the number isn't mistaken for real data.

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

### Which Tickers Get Priced

The priced ticker set is the **union of watchlist tickers and tickers with a non-zero position** — not the watchlist alone.

This matters because the two sets can diverge: a user (or the LLM) can remove a ticker from the watchlist while still holding shares of it. If pricing followed the watchlist alone, that position's current price, unrealized P&L, heatmap tile, and its contribution to total portfolio value would all go stale or null the moment it was removed.

Consequently:

- `DELETE /api/watchlist/{ticker}` **never** affects positions, and never stops pricing a held ticker
- `POST /api/portfolio/trade` on a ticker not in the watchlist adds it to the priced set (it is not auto-added to the watchlist — see §8)
- A ticker leaves the priced set only when it is both off the watchlist and at zero quantity
- Recomputing the union and calling `add_ticker`/`remove_ticker` on the data source is the backend's job on every watchlist and trade mutation

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API

**This section documents the shipped implementation in `backend/app/market/stream.py`. It is the frontend/backend contract — do not re-derive it.**

#### Frame shape

One frame carries **every priced ticker**, as a JSON object keyed by ticker symbol. It is *not* one event per ticker, and it is *not* a delta of only what changed.

```
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 190.52, "previous_price": 190.48, "timestamp": 1754159823.44, "change": 0.04, "change_percent": 0.021, "direction": "up"}, "GOOGL": {...}, ...}

data: {"AAPL": {...}, "GOOGL": {...}, ...}
```

- **No named event type.** Frames arrive on the default `message` event, so the client uses `es.onmessage`, not `es.addEventListener("price", …)`.
- **One `JSON.parse` per frame**, yielding roughly 2 updates/sec rather than 10+ discrete events/sec. The frontend should apply a frame as a single state update across all tickers.
- `timestamp` is **Unix epoch seconds as a float** (from `time.time()`), not an ISO string. It is the only place in the system that isn't ISO-8601 — the SQLite columns in §7 are ISO strings. Do not confuse the two.
- `change` / `change_percent` / `direction` are **tick-to-tick** — relative to `previous_price`, the immediately preceding tick. They drive the flash animation. They are **not** daily figures (see below).

#### Cadence and change detection

The generator wakes every 500ms and emits a frame only when `PriceCache.version` has advanced. Because the simulator ticks continuously, in practice a frame lands about every 500ms. If the source stalls, frames simply stop — which the client must treat as a stall, not as an error (nothing is pushed to say "no change").

#### Reconnection

- The server emits `retry: 1000` as its first frame, so `EventSource` reconnects after ~1s on drop
- `Last-Event-ID` is **not** used and would be meaningless — prices are a live snapshot, not a replayable log. A reconnecting client gets the current state on the next frame and needs no catch-up
- No heartbeat/comment frames are sent. The ~500ms full-snapshot cadence *is* the keepalive; an idle connection only occurs if the data source has stopped
- `X-Accel-Buffering: no` and `Cache-Control: no-cache` are set so proxies don't buffer the stream
- The connection status indicator (§2) maps to `EventSource.readyState`: `OPEN` → green, `CONNECTING` → yellow, `CLOSED` → red

#### Daily change % vs. tick change

The watchlist shows a **daily** change % (§10), but nothing in the price feed provides one — `previous_price` is the previous *tick*, milliseconds ago. A separate `previous_close` field is required, defined per source:

| Source | `previous_close` |
|---|---|
| Simulator | That ticker's seed price (`SEED_PRICES`, or the random $50–$300 draw for non-seeded tickers), fixed for the life of the process |
| Massive | The real previous session close from the API |

Exposing it under one field name means the frontend computes `(price - previous_close) / previous_close` identically in both modes and never branches on the data source. Note that in simulator mode this is "change since the backend started", not a real trading day — acceptable for a simulation, but it means restarting the container resets every daily change % to 0.

**This field does not exist yet.** `PriceUpdate` currently carries `ticker`, `price`, `previous_price`, and `timestamp` only. Adding `previous_close` is a change to completed, tested market-data code (`models.py`, `cache.py`, `simulator.py`, `massive_client.py`, plus their tests) and is the one piece of §6 that is not yet built.

---

## 7. Database

### SQLite with Startup Initialization

The backend creates the schema and seeds default data in its **FastAPI lifespan startup handler**, before the server accepts requests. If the file doesn't exist or tables are missing, they are created and seeded. This means:

- No separate migration step
- No manual database setup
- Fresh volumes start with a clean, seeded database automatically

Initialization is at startup, **not** lazily on first request. Lazy init cannot work here: the market data task needs the watchlist and the snapshot task needs positions, and both start before any HTTP request arrives. A "first request" code path would be unreachable branching that still has to be written, reasoned about, and tested. Startup order is: open DB → create/seed if needed → read watchlist and positions → compute the priced ticker set (§6) → `source.start(tickers)` → write an initial portfolio snapshot → serve.

### Conventions

These apply to every table below and to every API response. Both agents must apply them identically.

- **Timestamps** are ISO-8601 in **UTC with a `Z` suffix** (`2026-08-02T17:19:43.574Z`), stored as TEXT. Never local time. This makes lexicographic sort equal chronological sort, which is what the history queries rely on. (Exception: the SSE feed uses float epoch seconds — see §6.)
- **Money** is stored as `REAL` and **rounded after every write**: cash and `total_value` to 2 decimals, `avg_cost` to 4. Quantities round to 4 decimals. Rounding at the write boundary keeps float drift from accumulating across many fractional trades. The exact rule matters less than both agents using the same one.
- **`user_id`** is on every table, hardcoded to `"default"`. Single-user today; enables multi-user later without a schema migration.
- **Primary keys**: `watchlist` and `positions` use TEXT UUIDs (rows are addressed individually). `trades` and `portfolio_snapshots` are append-only logs always read in time order, so they use `INTEGER PRIMARY KEY AUTOINCREMENT` — SQLite's native rowid, which is smaller, faster, and naturally ordered, removing the need to sort by a TEXT timestamp.

### Schema

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
- `id` INTEGER PRIMARY KEY AUTOINCREMENT
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `side` TEXT (`"buy"` or `"sell"`)
- `quantity` REAL (fractional shares supported)
- `price` REAL (the fill price taken from the server-side price cache — see §8)
- `executed_at` TEXT (ISO timestamp, UTC `Z`)

**portfolio_snapshots** — Portfolio value over time (for P&L chart). Recorded every 30 seconds by a background task, immediately after each trade execution, and once at startup.
- `id` INTEGER PRIMARY KEY AUTOINCREMENT
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp, UTC `Z`)

The startup snapshot means the P&L chart has a data point immediately rather than being blank for the first 30 seconds. Snapshots only exist while the container runs, so a gap appears for any period the app was stopped; the frontend breaks the line rather than interpolating across gaps longer than ~2 minutes, so downtime doesn't render as a straight line implying a flat portfolio.

**Retention.** One row per 30s is ~2,880/day, ~1M/year, and the P&L chart would otherwise fetch the whole table on every page load. Two mitigations, both required: `GET /api/portfolio/history` takes `since`/`limit` parameters (§8), and a daily task deletes snapshots older than **30 days**.

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON — see below; null for user messages)
- `created_at` TEXT (ISO timestamp, UTC `Z`)

The **`actions`** column records what the backend actually executed on behalf of an assistant message, including failures. The frontend renders these inline as confirmation/error chips (§10), so the shape is a hard contract:

```json
{
  "trades": [
    {"ticker": "AAPL", "side": "buy", "quantity": 10, "status": "executed", "price": 190.52, "total": 1905.20},
    {"ticker": "TSLA", "side": "buy", "quantity": 500, "status": "failed",
     "error": {"code": "INSUFFICIENT_CASH", "message": "Need $125,000.00, have $8,094.80"}}
  ],
  "watchlist_changes": [
    {"ticker": "PYPL", "action": "add", "status": "executed"}
  ]
}
```

Every entry carries a `status` of `"executed"` or `"failed"`; failed entries carry an `error` object using the same codes as §8. Successful trades record the actual fill `price` and `total`, which are not known until execution. Empty arrays are omitted.

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
| GET | `/api/portfolio/history` | Portfolio value snapshots over time (for P&L chart). Query: `?since=<iso>&limit=<n>` (default: last 24h, limit 1000, newest-last) |

### Watchlist
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Current watchlist tickers with latest prices |
| POST | `/api/watchlist` | Add a ticker: `{ticker}` |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker (never affects positions — see §6) |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat` | Send a message, receive complete JSON response (message + executed actions) |
| GET | `/api/chat/history` | Prior conversation for restoring the panel on page load. Query: `?limit=<n>` (default 50, newest-last) |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (for Docker/deployment) |
| POST | `/api/reset` | Reset cash, positions, trades, snapshots, chat, and watchlist to seed state |

`GET /api/chat/history` exists so the persisted `chat_messages` table is actually reachable by the UI — without it, conversation survives a restart in the database but silently disappears from the screen.

`POST /api/reset` matters for a course project that gets demoed repeatedly: the alternative is `docker compose down && rm db/finally.db && docker compose up`. Because `db/` is a bind mount (§11), that manual path also works and should be documented in the README.

### Trade Execution Rules

- **Fill price comes from the server-side `PriceCache` at request time.** The client never supplies a price; a `price` field in the request body is ignored if present. This keeps the simulation honest — a client cannot choose its own fill.
- **Any valid ticker can be traded**, whether or not it is on the watchlist (the §10 trade bar is a free-text field). Trading a ticker adds it to the priced set but does **not** add it to the watchlist.
- **No short selling and no margin.** Sells are capped at the quantity held; buys are capped at available cash. A sell that would take a position below zero is rejected, not partially filled.
- **Quantity** must be a finite number `> 0`, rounded to 4 decimals. Zero, negative, `NaN`, and `Infinity` are rejected.
- **Ticker format** is `^[A-Z]{1,5}$` after trimming and uppercasing. There is no symbol whitelist — in simulator mode any conforming symbol is priceable (§6).
- A position that reaches exactly zero quantity is **deleted**, not kept as a zero row.
- Trades are **all-or-nothing per trade**; there are no partial fills.

### Error Responses

All error responses share one envelope:

```json
{"error": {"code": "INSUFFICIENT_CASH", "message": "Need $125,000.00, have $8,094.80"}}
```

`message` is human-readable and safe to show directly in the UI. `code` is stable and is what the frontend and tests branch on.

| Code | Status | Meaning |
|---|---|---|
| `INVALID_TICKER` | 400 | Fails `^[A-Z]{1,5}$` |
| `INVALID_QUANTITY` | 400 | Not a finite number > 0 |
| `INVALID_SIDE` | 400 | Not `"buy"` or `"sell"` |
| `INSUFFICIENT_CASH` | 400 | Buy cost exceeds cash balance |
| `INSUFFICIENT_SHARES` | 400 | Sell quantity exceeds position |
| `PRICE_UNAVAILABLE` | 503 | Ticker valid but no price cached yet (first moments after startup, or a just-added ticker before its first tick) — retryable |
| `TICKER_NOT_FOUND` | 404 | `DELETE /api/watchlist/{ticker}` for a ticker not on the watchlist |
| `TICKER_ALREADY_WATCHED` | 409 | `POST /api/watchlist` for a duplicate |
| `LLM_UNAVAILABLE` | 502 | OpenRouter call failed or returned unparseable output (§9) |

Malformed request bodies are handled by FastAPI's default 422 validation response and are not remapped.

`PRICE_UNAVAILABLE` is the one genuinely time-dependent error: a ticker added at T+0 has no price until the next tick ~500ms later. The frontend should treat 503 as "retry shortly", not as a user error.

---

## 9. LLM Integration

When writing code to make calls to LLMs, use the **`cerebras`** skill (`.claude/skills/cerebras/SKILL.md`) to call LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider. Structured Outputs should be used to interpret the results.

`OPENROUTER_API_KEY` is read from the `.env` file in the project root. It is optional — see §5. If it is absent, chat runs in mock mode and no network call is made.

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads the **last 20 messages** from the `chat_messages` table (bounded so context size and latency stay flat as history grows)
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via LiteLLM → OpenRouter, requesting structured output, using the `cerebras` skill
5. Parses the complete structured JSON response
6. Auto-executes any trades or watchlist changes specified in the response, collecting a per-action outcome
7. Stores the message and the resulting `actions` blob (§7) in `chat_messages`
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

**Trades execute independently and in order.** If the LLM returns three trades and the second fails validation, the first and third still execute. There is no rollback and no transaction spanning multiple trades — each is validated against the state left by the previous one. Every outcome, success or failure, is recorded in the `actions` blob (§7).

**The LLM's prose is written before execution, so it can be wrong.** The model produces `message` and `trades` in a single structured response, and execution happens afterwards at step 6. The model can therefore write "Done — I've bought 10 AAPL for you" for a trade that then fails on insufficient cash. **There is no second LLM call to reconcile this.** Instead:

- The authoritative record of what happened is the `actions` blob, never the prose
- The frontend renders each action as a success or failure chip *beside* the message (§10), so a failure is visible even when the text claims success
- Failure chips show the `message` from the error envelope (§8), which is already human-readable

A second LLM call would double latency and cost on every trade turn to fix wording that the chips already correct. The chips are also strictly more reliable: they reflect what the ledger did, not what the model predicted it would do.

To reduce the mismatch at the source, the system prompt instructs the model to describe trades as *intended* rather than completed ("I'll buy 10 AAPL" rather than "I've bought 10 AAPL"), and the portfolio context it receives includes the current cash balance so it can avoid proposing unaffordable trades in the first place.

### System Prompt Guidance

The LLM should be prompted as "FinAlly, an AI trading assistant" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven in responses
- Describe trades as intended, not completed (see above)
- Always respond with valid structured JSON

### Handling Bad LLM Output

If the response is unparseable, fails schema validation, or the API call errors, the backend returns `LLM_UNAVAILABLE` (502, §8) with a user-safe message. It does **not** retry automatically, does not partially apply a half-parsed response, and writes no `chat_messages` row for the failed turn — so a retry from the user starts clean rather than compounding a broken history.

### LLM Mock Mode

Mock mode is active when `OPENROUTER_API_KEY` is absent, or when `LLM_MOCK=true` overrides a present key (§5). It returns deterministic responses without any network call, enabling fast/free/reproducible E2E tests, development without an API key, and CI runs.

**The mock is a contract, not a single canned reply.** §12's E2E suite asserts that a chat message can execute a trade and render inline, so the mock must be able to produce trades. It is keyword-matched on the user's message, checked in order, first match wins:

| Trigger (case-insensitive substring) | Response |
|---|---|
| `buy` + a ticker symbol | `message` acknowledging the buy, plus one `trades` entry: that ticker, side `buy`, quantity 1 |
| `sell` + a ticker symbol | Same shape, side `sell`, quantity 1 |
| `watch` / `add` + a ticker symbol | `message` plus one `watchlist_changes` entry with action `add` |
| `portfolio` / `position` | A fixed analysis paragraph, no actions |
| *(no match)* | A fixed generic reply, no actions |

Mock responses go through the **same execution and validation path** as real ones — a mocked buy with insufficient cash still fails and still produces a failure chip. That keeps the mock honest: tests exercise the real trade logic, and only the model call is faked.

---

## 10. Frontend Design

### Layout

The frontend is a single-page application with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), daily change % (computed from `previous_close`, §6), and a sparkline mini-chart (accumulated from SSE since page load)
- **Main chart area** — larger chart for the currently selected ticker, with at minimum price over time. Clicking a ticker in the watchlist selects it here. Like the sparklines, this chart is built from SSE data accumulated since page load — there is no historical backfill endpoint, so it starts near-empty, fills in progressively, and resets on reload. Show an explicit "collecting data…" state rather than an empty chart in the first seconds.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss)
- **P&L chart** — line chart showing total portfolio value over time, using data from `portfolio_snapshots`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill.
- **AI chat panel** — docked/collapsible sidebar. Message input, scrolling conversation history (restored on load via `GET /api/chat/history`), loading indicator while waiting for LLM response. Trade executions and watchlist changes render as inline chips beneath the message, driven by the `actions` blob (§7) — green for `executed`, red for `failed` with the error message shown. The chips are authoritative; the message prose is not (§9).
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`. Each frame is a full snapshot of all tickers on the default `message` event — parse once and apply as a single batched state update (§6). Do not create one subscription per ticker.
- **Charting: Recharts, for all four visualizations** (sparkline, main chart, P&L line, portfolio treemap). One library, one styling model, one mental model. Recharts is SVG-based rather than canvas; at ~2 frames/sec across a watchlist of this size that is not a performance concern, and it is the deciding factor that Recharts has a `Treemap` component while Lightweight Charts does not — using Lightweight Charts would mean shipping a second library for the heatmap alone.
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it. Drive it from the `direction` field in the SSE payload.
- Cap the client-side sparkline/chart buffers (e.g. last 300 points per ticker) so a long-lived tab doesn't grow memory without bound.
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed
- Tailwind CSS for styling with a custom dark theme
- Multiple tabs are supported: each opens its own `EventSource` and keeps its own accumulated sparkline buffer, so charts will legitimately differ between tabs while prices, portfolio, and cash stay consistent (all server-derived).

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

### Persistence: Bind Mount

The SQLite database persists via a **bind mount** of the project's `db/` directory to `/app/db` in the container. The backend writes `finally.db` to that path, so the file is directly visible in the project directory.

A bind mount is chosen over a named Docker volume specifically because this is a teaching project: students can open the database with any SQLite tool, see it appear on first run, and reset the app with `rm db/finally.db`. With a named volume the file would be invisible outside Docker and resettable only via `docker volume rm`.

### Starting and Stopping

`docker compose up` is the one and only documented way to run the app — an identical command on macOS, Linux, and Windows.

```yaml
# docker-compose.yml
services:
  app:
    build: .
    ports: ["8000:8000"]
    volumes: ["./db:/app/db"]
    env_file: [.env]
```

```bash
docker compose up --build     # start (rebuilds if the image is stale)
docker compose down           # stop and remove the container
```

Data persists across `down`/`up` because it lives in `./db` on the host.

**No platform-specific start/stop scripts.** Four shell/PowerShell scripts wrapping a `docker run` line would need hand-syncing across two platforms for no capability compose doesn't already provide declaratively — and the project depends on compose regardless, since the E2E suite uses `test/docker-compose.test.yml`. One documented command beats four scripts that can drift.

The README should note the `.env` prerequisite (`cp .env.example .env`), and that the app is fully functional with an empty `.env` (§5).

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

**Environment**: Tests run with `LLM_MOCK=true` by default for speed and determinism, against the mock contract defined in §9.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell shares: cash increases, position updates or disappears
- Remove a held ticker from the watchlist: the position remains and keeps updating (§6)
- Rejected trade: buying beyond available cash surfaces the `INSUFFICIENT_CASH` message and leaves cash unchanged
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send a message, receive a response, trade execution chip appears inline
- AI chat failure path: a mocked trade that fails validation renders a red failure chip (§9)
- Connection status: when the stream route is made to fail, the indicator turns yellow/red

**On testing SSE reconnection.** Verifying genuine reconnection end-to-end means killing the container mid-test or tearing down the network at the browser level — slow and flaky in CI, for behaviour `EventSource` provides natively rather than behaviour this project wrote. Split it instead:

- **E2E** covers what the user actually sees: Playwright request interception fails `/api/stream/prices`, and the test asserts the connection status indicator changes and recovers. Deterministic and fast.
- **Backend integration test** covers the server side: a client disconnecting mid-stream terminates the generator cleanly (`request.is_disconnected()`), and a fresh connection immediately receives a full snapshot frame.

Same confidence, far less flake.

---

## 13. Resolution Log

*A documentation review raised 35 items (contradictions, undefined contracts, open questions, simplifications). All have been resolved and folded into §§1–12 above — this section records **what was decided and where it landed**, so the reasoning behind a spec choice is recoverable without re-litigating it.*

*The sections above are the contract. This log is history.*

### Contradictions resolved

| # | Issue | Resolution | Landed in |
|---|---|---|---|
| A1 | §9 named a "cerebras-inference" skill; the repo's skill is `cerebras` | Corrected, with the path to `SKILL.md` | §9 |
| A2 | Named volume vs. bind mount, described in adjacent sentences | **Bind mount** (`./db:/app/db`) — the DB file must be visible and deletable for a teaching project | §11 |
| A3 | `backend/db/` and root `db/` meant different things one path segment apart, and `backend/db/` fell outside the `packages = ["app"]` wheel build | DB code → `backend/app/db.py`; root `db/` is the only `db/` | §4 |
| A4 | "Lazy Initialization" heading vs. "startup (or first request)" body | **Startup, in the lifespan handler.** Lazy init cannot work — background tasks need the watchlist before any request | §7 |
| A5 | "Canvas-based (Lightweight Charts or Recharts)" — Recharts is SVG, and Lightweight Charts has no treemap | **Recharts for all four charts**; canvas requirement dropped | §10 |
| A6 | `OPENROUTER_API_KEY` marked Required, but two documented paths run without it | Optional; absent → mock mode. The app never refuses to start | §5 |

### Contracts pinned down

These were the items where the Frontend and Backend agents would each have invented an incompatible answer.

| # | Contract | Resolution | Landed in |
|---|---|---|---|
| B1 | SSE frame shape | **One frame = all tickers**, JSON object keyed by symbol, default `message` event. Documented verbatim from the shipped `stream.py`, with an example frame | §6 |
| B2 | Keepalive / reconnect | `retry: 1000` is sent; no heartbeat needed (the 500ms snapshot cadence *is* the keepalive); `Last-Event-ID` deliberately unused — prices aren't replayable | §6 |
| B3 | "Daily change %" had nothing backing it | New `previous_close` field: seed price (simulator) or real prior close (Massive), so the frontend never branches on source. **Not yet built — see Q1** | §6, §10 |
| B4 | Adding a non-default ticker | Already handled in shipped code: random $50–300 seed, `DEFAULT_PARAMS`, `CROSS_GROUP_CORR`. Documented, including that such prices are arbitrary | §6 |
| B5 | Removing a watchlist ticker you hold | Priced set = **watchlist ∪ non-zero positions**. `DELETE` never touches positions | §6 |
| B6 | No failure contract for trades | One error envelope, nine stable codes, status mapping table | §8 |
| B7 | Fill price source | Server-side `PriceCache` at request time; client-supplied prices ignored. No shorting, no margin, no partial fills | §8 |
| B8 | `chat_messages` persisted but unreachable by the UI | Added `GET /api/chat/history` | §8, §10 |
| B9 | `actions` blob had no schema | Full schema with per-action `status`, fill price, and error object | §7 |
| B10 | Timestamp and money precision unspecified | UTC with `Z` everywhere (except the SSE feed's epoch float, called out explicitly); rounding applied at every write | §7 |

### Questions answered

| # | Question | Answer | Landed in |
|---|---|---|---|
| C1 | LLM prose is written before trades execute, so it can claim a success that failed | **No second LLM call.** `actions` is authoritative; the UI renders success/failure chips beside the message. Prompt also asks for intent phrasing ("I'll buy") over completion phrasing | §9, §10 |
| C2 | Partial execution of a multi-trade response | Execute independently in order, no rollback, record every outcome | §9 |
| C3 | Unbounded conversation history | Last 20 messages | §9 |
| C4 | `portfolio_snapshots` grows without bound | `since`/`limit` params + 30-day retention | §7, §8 |
| C5 | Snapshot gaps and a blank cold-start chart | Snapshot at startup; frontend breaks the line across gaps >2min rather than implying a flat portfolio | §7 |
| C6 | Main chart has no history either | Accepted and stated explicitly, with a "collecting data…" state | §2, §10 |
| C7 | Mock LLM must be able to execute a trade for E2E | Keyword-matched contract table; mock output runs through the **real** validation path | §9 |
| C8 | No reset path for repeated demos | `POST /api/reset`, plus `rm db/finally.db` (which the bind mount makes possible) | §8 |
| C9 | `.env.example` listed but absent | Specified as a committed copy of the §5 block; still needs creating | §4, §5 |
| C10 | Multiple tabs | Supported; sparkline buffers legitimately differ, server-derived state stays consistent | §10 |
| C11 | "Functional on tablet" with no breakpoint | Three breakpoints with an explicit collapse order | §2 |

### Simplifications applied

| # | Simplification | Effect |
|---|---|---|
| D1 | `backend/db/` directory → `backend/app/db.py` | One module for six small tables; also fixes the wheel-packaging bug (A3) |
| D2 | Deleted lazy initialization | Removes an unreachable code path that still had to be written and tested |
| D3 | One charting library (Recharts) | Removes a dependency, a bundle cost, and cross-chart styling drift |
| D4 | Compose-only; **four platform scripts deleted** | Removes the two-platform sync burden. `docker compose up` is one identical command everywhere, and the project already depends on compose for E2E. **See Q2** |
| D5 | Integer PKs for `trades` / `portfolio_snapshots` | Native rowid ordering for append-only logs; no UUID generation, no TEXT-timestamp sort |
| D6 | `LLM_MOCK` demoted to an override | Both keys now follow one rule — absent → use the fake. One less primary code path |
| D8 | Split the SSE resilience E2E test | Indicator behaviour in Playwright, reconnection in a backend test — same confidence, far less CI flake |

### Deliberately not applied

**D7 — dropping `direction` from the SSE payload.** The proposal was to remove it as derivable from `price - previous_price`. On inspection it is already implemented, serialized, and covered by tests in completed market-data code (`models.py`, `test_models.py`). The saving is a few bytes per frame; the cost is churning a finished, reviewed subsystem. Keeping it. `direction` remains the field the frontend drives the flash animation from (§10).

---

### Open questions for the project owner

Four decisions worth confirming before the Backend and Frontend agents start, since each is cheap to change now and expensive later.

**Q1 — `previous_close` means editing completed market-data code.** B3's fix is the only resolution here that reaches into the subsystem marked "Complete, tested, reviewed" — it touches `models.py`, `cache.py`, `simulator.py`, `massive_client.py`, and their tests. The alternative is dropping the daily change % from the watchlist and showing only the tick change, which needs no code changes but loses a column that every real trading terminal has. Confirm the edit is in scope?

**Q2 — Deleting the four start/stop scripts is the most consequential change I made.** §11 originally promised `start_mac.sh` / `stop_mac.sh` / `start_windows.ps1` / `stop_windows.ps1`, and I replaced all four with `docker compose up`. The engineering case is clear-cut, but this is a *course* project, and the scripts may exist for pedagogical reasons the plan doesn't state — teaching `docker run` flags explicitly, or sparing students a compose install. Easy to reinstate if so.

**Q3 — `POST /api/reset` is a new endpoint, not in the original §8.** I added it because a repeatedly-demoed course project needs a reset that isn't "delete a file and restart". If you'd rather keep the API surface exactly as specified, the bind mount alone makes `rm db/finally.db` a workable documented path and the endpoint can be dropped.

**Q4 — Several numbers are my invention and are worth a sanity check.** Specifically: 20-message chat history, 30-day snapshot retention, history defaults of 24h/1000 points, a 300-point client-side chart buffer, and the 1280/1024/768px breakpoints. All are reasonable defaults, none are derived from a stated requirement.

One further observation, not a question: the simulator's seed prices (`seed_prices.py`) are labelled "as of project creation" — AAPL at $190, NVDA at $800. These drift further from reality over time. It doesn't affect correctness in simulator mode, but if a student compares a FinAlly price to a real quote the gap will be visible. Worth a line in the README noting simulator prices are illustrative.
