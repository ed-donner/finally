# FinAlly — AI Trading Workstation

## Project Specification

## 1. Vision

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

## 2. User Experience

### First Launch

The user runs a single command (`docker compose up -d`, or the provided start script). A browser opens to `http://localhost:8000`. No login, no signup. They immediately see:

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
│  └── /*              Static file serving        │
│                      (Next.js export)           │
│                                                 │
│  SQLite database (Docker volume)                │
│  Background tasks:                              │
│  ├── market data simulator / Massive poller     │
│  └── portfolio snapshot recorder (every 60s)    │
└─────────────────────────────────────────────────┘
```

- **Frontend**: Next.js with TypeScript, built as a static export (`output: 'export'`), served by FastAPI as static files
- **Backend**: FastAPI (Python), managed as a `uv` project
- **Database**: SQLite, single file `finally.db`, persisted in a Docker volume
- **Real-time data**: Server-Sent Events (SSE) — simpler than WebSockets, one-way server→client push, works everywhere
- **AI integration**: LiteLLM → OpenRouter (Cerebras for fast inference), with structured outputs for trade execution
- **Market data**: Environment-variable driven — simulator by default, real data via Massive API if key provided

**Static file routing:** FastAPI includes all API routers first, then mounts the Next.js export as the last route: `app.mount("/", StaticFiles(directory="static", html=True))`. This guarantees `/api/*` is never shadowed by static files and `/` serves `index.html`.

### Why These Choices

| Decision                | Rationale                                                                                     |
| ----------------------- | --------------------------------------------------------------------------------------------- |
| SSE over WebSockets     | One-way push is all we need; simpler, no bidirectional complexity, universal browser support  |
| Static Next.js export   | Single origin, no CORS issues, one port, one container, simple deployment                     |
| SQLite over Postgres    | No auth = no multi-user = no need for a database server; self-contained, zero config          |
| Single Docker container | Students run one command; no service orchestration                                            |
| uv for Python           | Fast, modern Python project management; reproducible lockfile; what students should learn     |
| Market orders only      | Eliminates order book, limit order logic, partial fills — dramatically simpler portfolio math |

---

## 4. Directory Structure

```
finally/
├── frontend/                 # Next.js TypeScript project (static export)
├── backend/                  # FastAPI uv project (Python)
│   └── app/                  # The only installable package (hatch packages = ["app"])
│       ├── market/           # Market data (complete — see MARKET_DATA_SUMMARY.md)
│       └── db/               # Schema SQL, seed data, connection helpers
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start_mac.sh          # Thin wrapper: docker compose up (macOS/Linux)
│   └── stop_mac.sh           # Thin wrapper: docker compose down (macOS/Linux)
├── test/                     # Playwright E2E tests (run from the host)
├── db/                       # Local-dev SQLite location (not used by Docker)
│   └── .gitkeep              # finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── docker-compose.yml        # Primary way to run the app (all platforms)
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. All code lives inside the `app` package so it is importable from the installed wheel.
- **`backend/app/db/`** contains schema SQL definitions and seed logic. The backend initializes the database at startup — creating tables and seeding default data if the SQLite file doesn't exist or is empty.
- **`db/`** at the top level is where the SQLite file lives during local development (running `uv run uvicorn …` outside Docker). In Docker, the database lives in the named volume `finally-data` mounted at `/app/db`.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests. Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- **`scripts/`** contains thin start/stop wrappers around `docker compose`.

---

## 5. Environment Variables

```bash
# OpenRouter API key for LLM chat. The app starts without it; only chat is disabled.
OPENROUTER_API_KEY=your-openrouter-api-key-here

# Optional: Massive (Polygon.io) API key for real market data
# If not set, the built-in market simulator is used (recommended for most users)
MASSIVE_API_KEY=

# Optional: Set to "true" for deterministic mock LLM responses (testing, no API key needed)
LLM_MOCK=false

# Optional: SQLite file path. Defaults to <project root>/db/finally.db for local dev;
# the Dockerfile sets DB_PATH=/app/db/finally.db.
DB_PATH=
```

### Behavior

- If `MASSIVE_API_KEY` is set and non-empty → backend uses Massive REST API for market data
- If `MASSIVE_API_KEY` is absent or empty → backend uses the built-in market simulator
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses (see Section 9)
- If `OPENROUTER_API_KEY` is missing and `LLM_MOCK` is not `true` → the app starts normally; `POST /api/chat` returns `503` with `{"detail": "AI chat is not configured. Set OPENROUTER_API_KEY in .env (or LLM_MOCK=true)."}` and the chat panel shows that message. No silent fallback to mock.
- **The backend reads configuration from environment variables only.** In Docker they are supplied via an optional `env_file` (`.env`, `required: false`) plus an explicit `LLM_MOCK` passthrough in `docker-compose.yml` (§12). For local development, the backend calls `python-dotenv`'s `load_dotenv()` on the project-root `.env` at startup; this is a no-op in the container where no `.env` file exists.

---

## 6. Market Data

The market data subsystem is **complete** in `backend/app/market/`. See `planning/MARKET_DATA_SUMMARY.md` for the architecture, the `MarketDataSource` interface, `PriceCache` API, simulator details, and Massive client. This section defines only the contract the rest of the app builds against, plus the changes the rest of the app requires.

### Interface Contract

- `create_market_data_source(cache)` returns a simulator (default) or Massive poller (`MASSIVE_API_KEY` set). Both write to a shared in-memory `PriceCache`.
- `source.add_ticker(t)` / `source.remove_ticker(t)` change the tracked set. The simulator seeds a price into the cache **immediately** on `add_ticker` (unknown tickers get a random seed price between $50 and $300). Massive only has a price after its next poll (up to 15s on the free tier).
- `cache.get_price(t)` returns `float | None`. `None` means "no price yet" and must be handled by every consumer.

### Tracked Tickers = Watchlist ∪ Held Positions

The set of tickers the market data source tracks is the union of the watchlist and all tickers with an open position. Removing a ticker from the watchlist hides it from the watchlist panel, but if the user still holds it, it stays tracked so the position can be valued. A ticker is removed from the market data source only when it is in neither set (e.g. after removing it from the watchlist while flat, or after fully selling a position that isn't on the watchlist). On startup, the backend loads both sets from the database and calls `source.start(watchlist ∪ positions)`.

### Ticker Validation

Tickers are normalized to uppercase and trimmed, then must match `^[A-Z]{1,5}$`. Anything else is rejected with `400`. There is no whitelist: in simulator mode any valid symbol gets a price; in Massive mode a symbol the API doesn't recognize simply never receives a price (it shows as `—` in the UI, and trades on it return `409`).

### Session Change % (required change to the market module)

The cache's `change_percent` is tick-to-tick and near zero, so it is not useful for display. The `PriceCache` must record an **`open_price`** per ticker — the first price written for that ticker since it started being tracked in this process — and `PriceUpdate.to_dict()` must add `open_price` and `session_change_percent` (`(price - open_price) / open_price * 100`, rounded to 4 dp). The watchlist column is labelled **"Chg %"** (not "Daily") and shows `session_change_percent`.

### SSE Streaming

- Endpoint: `GET /api/stream/prices` (`text/event-stream`); client uses native `EventSource`
- The server first sends `retry: 1000` so the browser reconnects 1s after a drop
- Every ~500ms the server checks the cache; **if anything changed** it sends **one event containing all tracked tickers**, keyed by ticker. If nothing changed (e.g. between Massive polls), nothing is sent.
- Event payload (one `data:` line, JSON):

```json
{
  "AAPL": {
    "ticker": "AAPL",
    "price": 190.5,
    "previous_price": 190.42,
    "open_price": 190.0,
    "timestamp": 1760000000.123,
    "change": 0.08,
    "change_percent": 0.042,
    "session_change_percent": 0.2632,
    "direction": "up"
  },
  "GOOGL": { "...": "..." }
}
```

- `timestamp` is **Unix seconds (float)**; all REST endpoints use ISO 8601 UTC strings.
- `direction` is `"up"`, `"down"`, or `"flat"` (tick-to-tick) and drives the flash animation.
- Because the payload includes all tracked tickers, it may include held tickers that aren't on the watchlist. The frontend uses those prices for valuation but only renders watchlist tickers in the watchlist panel.

### Massive Free Tier Note

With the Massive free tier (5 calls/min) prices update every 15 seconds, so the UI looks fairly static (few flashes, slow sparklines). Correlated moves and random events are simulator-only. The README must say this so users aren't surprised.

---

## 7. Database

### SQLite Initialization & Access

At startup, the backend opens the SQLite file at `DB_PATH` (creating parent directories if needed). If the file doesn't exist or tables are missing, it creates the schema and seeds default data. This means:

- No separate migration step
- No manual database setup
- Fresh Docker volumes start with a clean, seeded database automatically

Concurrency rules (the snapshot task, chat handler, and trade endpoint all write):

- **WAL mode** enabled on initialization (`PRAGMA journal_mode=WAL`)
- **One connection per operation** (open, use, close); no shared long-lived connection across tasks
- **Trade execution runs in a single transaction** started with `BEGIN IMMEDIATE`: read cash and position → validate → upsert/delete position → insert trade → update cash → commit. This prevents races between a manual trade and an LLM trade.

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

**positions** — Current holdings (one row per ticker per user). A position whose quantity falls below `1e-9` after a sell is **deleted**, not stored as zero.

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

**portfolio_snapshots** — Portfolio value over time (for P&L chart). `total_value` = cash + Σ(quantity × current price), i.e. **includes cash**. Recorded once at startup, every **60 seconds** by a background task, and immediately after each successful trade.

- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp)

**chat_messages** — Conversation history with LLM

- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON `{"trades": [...], "watchlist_changes": [...]}` with per-item results, same shape as the `/api/chat` response; null for user messages)
- `created_at` TEXT (ISO timestamp)

### Default Seed Data

- One user profile: `id="default"`, `cash_balance=10000.0`
- Ten watchlist entries: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX

---

## 8. Trading Rules

These rules apply identically to manual trades (`POST /api/portfolio/trade`) and LLM trades.

- **Ticker**: normalized and validated per Section 6. **Buying** a ticker that is not on the watchlist is allowed; it is **automatically added to the watchlist** first (so it starts being tracked and appears in the UI). Sells never touch the watchlist: a sell requires a held position, and held tickers are already tracked (§6), so fully selling an off-watchlist position untracks it rather than re-adding it.
- **Quantity**: must be a finite number `> 0`. No minimum lot size. Fractional shares allowed.
- **Price**: the current cache price, **read once** and used both for validation and as the recorded trade price. If there is no price yet (`None`), the trade is rejected with `409` `{"detail": "No price available for XYZ yet. Try again shortly."}` and no trade, position, cash, or snapshot rows are written.
- **Buy**: requires `quantity × price ≤ cash` (with a `1e-9` tolerance), else `400` `"Insufficient cash"`. Updates `avg_cost` as a weighted average: `(old_qty × old_avg + qty × price) / (old_qty + qty)`.
- **Sell**: requires `quantity ≤ held quantity` (with a `1e-9` tolerance), else `400` `"Insufficient shares"`. `avg_cost` is unchanged. If the remaining quantity is `< 1e-9`, the position row is deleted. Selling a ticker you don't hold is `400`.
- **Realized P&L** is not tracked (out of scope). Unrealized P&L = `(current_price − avg_cost) × quantity`.
- **Rounding**: store full-precision floats; round only for display (prices and money to 2 dp, quantities to up to 4 dp).
- A successful trade inserts a `trades` row and records a portfolio snapshot.

### Trade Execution Order

Every trade (manual or LLM) runs these steps in order. The async `source.add_ticker` call can't happen inside the SQLite transaction, so the watchlist auto-add is a separate step that comes before it:

1. **Validate input**: normalize the ticker, check quantity and side. On failure, return `400` and write nothing.
2. **Auto-add (buys only)**: if the ticker is not on the watchlist, insert the watchlist row and `await source.add_ticker(ticker)`. **This persists even if the trade later fails** (409, insufficient cash, etc.); the user simply ends up watching the ticker. In simulator mode a price is available immediately. In Massive mode the trade typically returns `409` until the next poll.
3. **Read the price** once from the cache. If it is `None`, return `409`.
4. **Transaction**: `BEGIN IMMEDIATE` → read cash and position → validate (insufficient cash or shares → rollback, `400`) → upsert/delete the position → insert the trade → update cash → commit.
5. **Snapshot**: record a portfolio snapshot.
6. **Untrack**: if the position was closed and the ticker is not on the watchlist, `await source.remove_ticker(ticker)`.

In chat, each action runs this sequence independently, so a failed watchlist change has no effect on a following trade. A buy still auto-adds its own ticker.

---

## 9. API Endpoints

All REST responses are JSON. Timestamps are ISO 8601 UTC strings. Prices that aren't available yet are `null`.

**Errors** use FastAPI's default shape `{"detail": "human-readable message"}`:

- `400` — business validation failure (invalid ticker, insufficient cash/shares, quantity ≤ 0)
- `404` — not found (e.g. deleting a ticker not on the watchlist)
- `409` — price not yet available
- `422` — malformed request body (FastAPI's automatic validation; here `detail` is a list — the frontend should fall back to a generic message)
- `502` — LLM call failed or returned unparseable output
- `503` — chat not configured (no API key)

### Portfolio Object

Returned by `GET /api/portfolio` and embedded in trade and chat responses:

```json
{
  "cash": 8099.0,
  "positions_value": 1905.0,
  "total_value": 10004.0,
  "unrealized_pnl": 4.0,
  "positions": [
    {
      "ticker": "AAPL",
      "quantity": 10,
      "avg_cost": 190.1,
      "current_price": 190.5,
      "market_value": 1905.0,
      "unrealized_pnl": 4.0,
      "unrealized_pnl_percent": 0.2104,
      "weight": 0.1904
    }
  ]
}
```

`weight` = market value / total value (including cash). If a position's `current_price` is `null` (e.g. Massive hasn't polled yet after a restart), it is valued at `avg_cost` and its P&L fields are `0`.

### Market Data

| Method | Path                 | Description                                 |
| ------ | -------------------- | ------------------------------------------- |
| GET    | `/api/stream/prices` | SSE stream of live price updates (see §6)   |

### Portfolio

| Method | Path                     | Request                     | Response                                                                  |
| ------ | ------------------------ | --------------------------- | ------------------------------------------------------------------------- |
| GET    | `/api/portfolio`         | —                           | Portfolio object                                                          |
| POST   | `/api/portfolio/trade`   | `{ticker, quantity, side}`  | `{"trade": {id, ticker, side, quantity, price, executed_at}, "portfolio": {...}}` |
| GET    | `/api/portfolio/history` | —                           | `{"snapshots": [{"total_value": 10000.0, "recorded_at": "..."}]}` — most recent 500, oldest first |

### Watchlist

| Method | Path                      | Request    | Response                                                                                 |
| ------ | ------------------------- | ---------- | ---------------------------------------------------------------------------------------- |
| GET    | `/api/watchlist`          | —          | `{"tickers": [{ticker, added_at, price, previous_price, open_price, session_change_percent, direction}]}` (in `added_at` order) |
| POST   | `/api/watchlist`          | `{ticker}` | `201` with the watchlist item. Idempotent: adding an existing ticker returns `200` with the existing item. |
| DELETE | `/api/watchlist/{ticker}` | —          | `204`. `404` if not on the watchlist. Does not affect positions (§6).                    |

### Chat

| Method | Path                | Request     | Response                                                    |
| ------ | ------------------- | ----------- | ----------------------------------------------------------- |
| POST   | `/api/chat`         | `{message}` | Chat response (see §10)                                     |
| GET    | `/api/chat/history` | —           | `{"messages": [{id, role, content, actions, created_at}]}` — most recent 50, oldest first. Used to restore the chat panel on page load. |

### System

| Method | Path          | Description                                      |
| ------ | ------------- | ------------------------------------------------ |
| GET    | `/api/health` | `{"status": "ok"}` (for Docker/deployment)       |

---

## 10. LLM Integration

When writing code to make calls to LLMs, use cerebras-inference skill to use LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider. Structured Outputs should be used to interpret the results.

There is an OPENROUTER_API_KEY in the .env file in the project root.

**Provider fallback:** the request sets `provider.order = ["cerebras"]` (per the skill) and leaves OpenRouter's default fallback enabled, so if Cerebras is unavailable OpenRouter routes to another provider. Slower, but chat keeps working.

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads the **last 20** messages from `chat_messages`. For assistant messages with actions, a compact summary of the action results is appended to the content (e.g. `[Actions: buy 10 AAPL @ 190.10 executed; sell 5 TSLA failed: Insufficient shares]`) so the LLM sees the outcome of its previous actions.
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via LiteLLM → OpenRouter, requesting structured output, using the cerebras-inference skill. The call **must be async**: `await litellm.acompletion(..., timeout=30)`. The skill's examples use synchronous `litellm.completion`, which would block the event loop and freeze SSE, the simulator, and all other requests for the whole call. A timeout or any other LLM exception maps to `502`.
5. Parses the complete structured JSON response
6. Executes watchlist changes, then trades, **sequentially and independently**: each one is validated and applied on its own (Section 8). A failure doesn't stop the others; each item gets its own `status` and `error`.
7. Stores the user message and the assistant message (with action results in `actions`) in `chat_messages`
8. Returns the chat response to the frontend (no token-by-token streaming — Cerebras inference is fast enough that a loading indicator is sufficient)

There is **no second LLM call** after execution. If a trade fails, the LLM's message may be optimistic ("Done, bought 10 AAPL"). The frontend renders the per-action result (✓ / ✗ with error) inline under the message, so the user always sees what actually happened. The LLM sees failures on the next turn through the history (step 2).

If the LLM call fails or its output can't be parsed into the schema, the endpoint returns `502` and stores nothing.

### Structured Output Schema (LLM → backend)

The LLM is instructed to respond with JSON matching this schema. **All three fields are required**; `trades` and `watchlist_changes` may be empty arrays.

```json
{
  "message": "Your conversational response to the user",
  "trades": [{ "ticker": "AAPL", "side": "buy", "quantity": 10 }],
  "watchlist_changes": [{ "ticker": "PYPL", "action": "add" }]
}
```

- `message`: The conversational text shown to the user
- `trades`: Trades to auto-execute. `side` is `"buy"` or `"sell"`.
- `watchlist_changes`: Watchlist modifications. `action` is `"add"` or `"remove"`.

Define these as Pydantic models (with `Literal` types for `side`/`action`) and pass the model as `response_format`.

### Chat Response (backend → frontend)

```json
{
  "message": "Bought 10 AAPL and added PYPL to your watchlist.",
  "trades": [
    {
      "ticker": "AAPL",
      "side": "buy",
      "quantity": 10,
      "price": 190.1,
      "status": "executed",
      "error": null
    }
  ],
  "watchlist_changes": [
    { "ticker": "PYPL", "action": "add", "status": "executed", "error": null }
  ],
  "portfolio": { "...": "Portfolio object (§9), reflecting any executed trades" }
}
```

`status` is `"executed"` or `"failed"`; on failure `price` is `null` and `error` holds the same message the REST endpoint would return. The `actions` column in `chat_messages` stores `{"trades": [...], "watchlist_changes": [...]}` in exactly this shape.

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:

- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

### System Prompt Guidance

The LLM should be prompted as "FinAlly, an AI trading assistant" with instructions to:

- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven in responses
- Always respond with valid structured JSON

### LLM Mock Mode

When `LLM_MOCK=true`, the backend skips OpenRouter and builds the structured response deterministically from the user's message, then runs it through the **same execution path** as real responses. Rules (case-insensitive, all matches applied in order of appearance):

- `buy <N> <TICKER>` / `sell <N> <TICKER>` (N may be decimal) → a trade
- `add <TICKER>` / `remove <TICKER>` → a watchlist change
- `message` is `"Mock response: <summary of parsed actions>"` if any action matched, otherwise exactly `"Mock response: I am FinAlly, your AI trading assistant."`

Example: `"please buy 5 AAPL and add PYPL"` → one buy of 5 AAPL, one watchlist add of PYPL. This enables fast, free, reproducible E2E tests, development without an API key, and CI.

---

## 11. Frontend Design

### Layout

The frontend is a single-page application with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), **Chg %** (`session_change_percent`, §6), and a sparkline mini-chart (accumulated from SSE since page load). Tickers without a price yet show `—`.
- **Main chart area** — larger chart for the currently selected ticker, with at minimum price over time. Clicking a ticker in the watchlist selects it here.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss)
- **P&L chart** — line chart showing total portfolio value over time, from `GET /api/portfolio/history`. Lowest-priority UI element: build it last.
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill. Errors (`detail`) shown inline.
- **AI chat panel** — docked/collapsible sidebar. Message input, scrolling conversation history (restored from `GET /api/chat/history` on load), loading indicator while waiting for LLM response. Trade executions and watchlist changes shown inline under each assistant message with ✓/✗ status and error text.
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Data Flow

- On load: `GET /api/portfolio`, `GET /api/watchlist`, `GET /api/portfolio/history`, `GET /api/chat/history`, and open the SSE stream.
- After a trade or chat response: replace portfolio state with the `portfolio` object returned in the response (no follow-up GET). After **every successful trade and every chat response**, re-fetch `GET /api/watchlist`, because buys can auto-add tickers (§8) even when the chat response has no `watchlist_changes`.
- **Live total value** is computed on the frontend: `cash + Σ(quantity × latest SSE price)` (falling back to the server's `current_price`/`avg_cost` when no SSE price is known). No backend push is needed.
- **Price history buffer**: the frontend keeps up to **500** points per ticker from the SSE stream (ring buffer, dropping the oldest). The main chart uses the whole buffer; sparklines use the last 100 points.

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`. Connection status: `open` → green; `error` while `readyState === CONNECTING` → yellow; `readyState === CLOSED` → red.
- **Charts: Recharts** for the main price chart, the P&L chart, and the heatmap (`Treemap`). Sparklines are a tiny hand-rolled SVG `<polyline>` (no library).
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed
- Tailwind CSS for styling with a custom dark theme

---

## 12. Docker & Deployment

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
  - ENV DB_PATH=/app/db/finally.db
  - Expose port 8000
  - CMD: uvicorn serving FastAPI app
```

FastAPI serves the static frontend files and all API routes on port 8000.

### docker-compose.yml (primary run path, all platforms)

```yaml
services:
  app:
    build: .
    image: finally
    container_name: finally
    ports: ["8000:8000"]
    env_file:
      - path: .env
        required: false   # app must start without .env (§5); needs Compose ≥ 2.24
    environment:
      - LLM_MOCK=${LLM_MOCK:-false}   # lets `LLM_MOCK=true docker compose up` work for E2E
    volumes: ["finally-data:/app/db"]
volumes:
  finally-data:
```

- `.env` is optional. Without it the app starts with the simulator, and chat returns `503` (§5).
- `LLM_MOCK` is passed through explicitly because `env_file` only reads the file and never sees shell variables. Compose interpolation reads both the shell and `.env`, so setting `LLM_MOCK=true` in either place works. `environment` takes precedence over `env_file`.

- Start: `docker compose up -d` (add `--build` to rebuild). Works on macOS, Linux, and Windows.
- Stop: `docker compose down` — the named volume `finally-data` (and the database) persists. `docker compose down -v` resets all data.

### Start/Stop Scripts

**`scripts/start_mac.sh`** (macOS/Linux): runs `docker compose up -d` (passing `--build` through if given), prints `http://localhost:8000`, and opens the browser if `open`/`xdg-open` is available.

**`scripts/stop_mac.sh`** (macOS/Linux): runs `docker compose down` (never `-v`).

There are no Windows scripts; the README tells Windows users to run the `docker compose` commands directly. Both scripts are idempotent.

### Optional Cloud Deployment

The container is designed to deploy to AWS App Runner, Render, or any container platform. A Terraform configuration for App Runner may be provided in a `deploy/` directory as a stretch goal, but is not part of the core build.

---

## 13. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**:

- Market data: already covered (73 tests). Add tests for `open_price` / `session_change_percent`.
- Portfolio: trade execution logic, weighted avg cost, P&L calculations, edge cases (selling more than owned, buying with insufficient cash, selling at a loss, selling the full position deletes the row, float-tolerance boundaries, no price → 409)
- Tracked tickers: removing a held ticker from the watchlist keeps it tracked; selling it out while off-watchlist untracks it
- LLM: structured output parsing handles all valid schemas, graceful handling of malformed responses (502), per-trade independent execution within chat flow, mock-mode parsing rules
- API routes: correct status codes, response shapes, error handling

**Frontend (Vitest)** — focus on pure logic, not rendering or snapshots:

- P&L and live total-value calculations
- Price history ring buffer (cap at 500, sparkline slice)
- Flash-class trigger (up/down/flat → class)
- Connection-status state transitions from `EventSource` events
- API response parsing (portfolio object, chat action results)

### E2E Tests (in `test/`)

**Infrastructure**: Playwright runs **on the host** (`npx playwright install` locally or in CI) against the app started fresh in mock mode:

```bash
docker compose down -v                      # empty volume → seeded DB, $10k cash
LLM_MOCK=true docker compose up -d --build
npx playwright test
```

No separate Playwright container. The `down -v` matters because "Fresh start" scenarios assume a newly seeded database.

**Key Scenarios**:

- Fresh start: default watchlist appears, $10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell shares: cash increases, position updates or disappears
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send `buy 5 AAPL`, receive a response, trade execution appears inline with ✓ and the position appears

SSE reconnection is covered by the frontend connection-status unit test rather than E2E.
