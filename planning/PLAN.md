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
- **Click a ticker** to see a larger detailed chart in the main chart area — like the sparklines, this chart is built from prices accumulated on the frontend since page load, so it starts empty and fills in progressively. No price history is persisted server-side.
- **Buy and sell shares** — market orders only, instant fill at current price, no fees, no confirmation dialog
- **Monitor their portfolio** — a heatmap (treemap) showing positions sized by weight and colored by P&L, plus a P&L chart tracking total portfolio value over time
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, % change
- **Chat with the AI assistant** — ask about their portfolio, get analysis, and have the AI execute trades and manage the watchlist through natural language
- **Manage the watchlist** — add/remove tickers manually or via the AI chat

### Visual Design

- **Dark theme**: backgrounds around `#0d1117` or `#1a1a2e`, muted gray borders, no pure black
- **Price flash animations**: brief green/red background highlight on price change, fading over ~500ms via CSS transitions
- **Connection status indicator**: a small colored dot in the header with two states — green when a price message has arrived in the last 10 seconds, red otherwise. `EventSource` retries forever on its own, so there is no separate "reconnecting" state to detect.
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
- **AI integration**: LiteLLM → OpenRouter using a free model, with forced tool calling for trade execution
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
| Free LLM model | The app must cost nothing to run. This rules out paid inference providers, and with them Structured Outputs — hence forced tool calling (§9) |

---

## 4. Directory Structure

```
finally/
├── frontend/                 # Next.js TypeScript project (static export)
├── backend/                  # FastAPI uv project (Python)
│   └── app/
│       ├── market/           # Market data (built) — see MARKET_DATA_SUMMARY.md
│       └── database/         # Schema definitions, seed data, connection handling
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start_mac.sh          # Launch Docker container (macOS/Linux)
│   ├── stop_mac.sh           # Stop Docker container (macOS/Linux)
│   ├── start_windows.ps1     # Launch Docker container (Windows PowerShell)
│   └── stop_windows.ps1      # Stop Docker container (Windows PowerShell)
├── test/                     # Playwright E2E tests + docker-compose.test.yml
├── db/                       # Bind-mounted into the container (finally.db lives here at runtime)
│   └── .gitkeep              # Directory exists in repo; finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

Only `backend/` and `planning/` exist today. The rest is the intended layout, created by
agents as each component is built.

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/app/database/`** contains schema SQL definitions and seed logic. The backend lazily initializes the database on first request — creating tables and seeding default data if the SQLite file doesn't exist or is empty. (Named `database/`, not `db/`, so it is never confused with the runtime `db/` directory below.)
- **`db/`** at the top level is bind-mounted to `/app/db` in the container. The SQLite file (`db/finally.db`) is created here by the backend and persists across container restarts. A bind mount rather than a named volume, so the database file is directly visible and inspectable on the host.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests and supporting infrastructure (e.g., `docker-compose.test.yml`). Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- **`scripts/`** contains start/stop scripts that wrap Docker commands.

---

## 5. Environment Variables

```bash
# Required: OpenRouter API key for LLM chat functionality
OPENROUTER_API_KEY=your-openrouter-api-key-here

# Optional: which OpenRouter model to use. Must be a free model (":free" suffix).
# Defaults to nvidia/nemotron-3.5-lightning:free if unset.
OPENROUTER_MODEL=openrouter/nvidia/nemotron-3.5-lightning:free

# Optional: Massive (Polygon.io) API key for real market data
# If not set, the built-in market simulator is used (recommended for most users)
MASSIVE_API_KEY=

# Optional: Set to "true" for deterministic mock LLM responses (testing)
LLM_MOCK=false
```

Variable names are **case-sensitive**. Docker passes `.env` through verbatim with
`--env-file`, so a lowercase `openrouter_api_key` works on Windows (where Python normalises
environment keys to uppercase) but fails inside the Linux container. Write them uppercase.

### Behavior

- If `MASSIVE_API_KEY` is set and non-empty → backend uses Massive REST API for market data
- If `MASSIVE_API_KEY` is absent or empty → backend uses the built-in market simulator
- If `OPENROUTER_MODEL` is set → that model is used; otherwise the default free model
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses (for E2E tests)

### Where `.env` Is Read

`.env` lives at the **project root**, one level above the `backend/` uv project. The two
runtimes reach it differently:

- **In Docker**: no file is read. The start scripts pass `--env-file .env` and the values
  arrive as real environment variables.
- **Locally**: the backend calls `load_dotenv(Path(__file__).parents[2] / ".env")` at
  startup, so `uv run` from inside `backend/` still finds the root `.env`.

Both paths end at `os.environ`, so application code only ever reads environment variables.

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

**Unknown tickers.** The user (or the AI) can add any symbol to the watchlist, including
ones with no entry in `seed_prices.py`. The simulator assigns such a ticker a fallback
seed — a random price in the $50-$500 range with default drift and volatility, and no
correlation group — rather than rejecting it. This keeps the watchlist open-ended without
needing a symbol database.

**Restart behaviour.** The simulator holds no state across restarts: it always begins from
seed prices. Positions and cash persist in SQLite, so after a restart a holding reprices to
its seed level — a discontinuity that would show as a cliff in the P&L chart. To avoid
charting two incomparable price regimes on one line, **`portfolio_snapshots` is cleared on
startup in simulator mode** (§7). Positions, cash, trades and the watchlist are untouched;
only the value-over-time series restarts.

### Massive API (Optional)

- REST API polling (not WebSocket) — simpler, works on all tiers
- **One grouped request per poll** covering every tracked ticker — not one request per ticker. This is what keeps the free tier viable: 1 call per 15s is 4 calls/min against a 5 call/min limit, regardless of how many tickers are tracked (see the watchlist cap in §8)
- Free tier (5 calls/min): poll every 15 seconds
- Paid tiers: poll every 2-15 seconds depending on tier
- Parses REST response into the same format as the simulator

**Price staleness is accepted, not guarded.** On the free tier a cached price can be up to
15 seconds old, and outside market hours it is frozen at the last close. Trades fill at
whatever the cache holds. There is no staleness check and no market-hours logic — this is a
simulated portfolio, and the simulator is the default path.

### Shared Price Cache

- A single background task (simulator or Massive poller) writes to an in-memory price cache
- The cache holds the latest price, previous price, previous close, and timestamp for each ticker
- SSE streams read from this cache and push updates to connected clients
- This architecture supports future multi-user scenarios without changes to the data layer

### Previous Close and Daily Change

The watchlist shows a **daily change %**, which needs a baseline the tick-over-tick
`previous_price` cannot provide. Each ticker therefore carries a `prev_close`:

- **Simulator**: `prev_close` is defined per ticker in `seed_prices.py`, a few percent away
  from the seed price so the watchlist opens with a realistic spread of gainers and losers.
  A fallback ticker gets `prev_close` equal to its generated seed price.
- **Massive**: `prev_close` comes from the API's previous-close field on each poll.

`PriceUpdate` exposes `change_from_close` and `change_percent_from_close` alongside the
existing tick-level `change` / `change_percent`. The watchlist's "Change %" column uses the
close-based value; the green/red price flash uses the tick-level direction.

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- Server pushes price updates for every **tracked ticker** at a regular cadence (~500ms)
- Each SSE event contains ticker, price, previous price, previous close, timestamp, and change direction
- Client handles reconnection automatically (EventSource has built-in retry)

### The Tracked Ticker Set

The set of tickers being priced is **the watchlist plus every ticker with a non-zero
position** — not the watchlist alone. A user can remove a ticker from their watchlist while
still holding it, and the portfolio cannot be valued without a live price for it.

Consequences the watchlist routes must honour:

- Adding a ticker calls `source.add_ticker()` in addition to the database insert. A database
  write on its own does not start pricing the symbol.
- Removing a ticker calls `source.remove_ticker()` and `cache.remove()` **only if no open
  position exists** for it. Otherwise it stays tracked and simply stops being displayed in
  the watchlist panel.
- Opening a position in a ticker that is not on the watchlist adds it to the tracked set.

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

There is deliberately **no `realized_pnl` column**. When a position is sold, the gain or
loss flows into `cash_balance`, and total portfolio value (cash + positions) already
reflects it. Realized P&L is not displayed separately anywhere in the UI.

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

A fully-sold position is **deleted**, never kept at quantity 0. A row in `positions` always
means an open holding, so the positions table and heatmap need no zero-filtering.

**trades** — Trade history (append-only audit log)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `side` TEXT (`"buy"` or `"sell"`)
- `quantity` REAL (fractional shares supported)
- `price` REAL
- `executed_at` TEXT (ISO timestamp)

Read by `GET /api/trades` and displayed in the trade blotter panel (§10). Append-only —
trades are never updated or deleted, so the blotter is a true audit trail of everything that
happened, including trades the AI executed.

**portfolio_snapshots** — Portfolio value over time (for P&L chart). Recorded every 60 seconds by a background task, and immediately after each trade execution.
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp)

The snapshot task **skips a tick if any held ticker has no price in the cache**, so the
chart never records a portfolio valued at zero during the first seconds after startup. A
100% cash portfolio is always valuable and is recorded normally (a flat line at $10,000).

**This table is cleared on startup in simulator mode.** The simulator restarts from seed
prices (§6), so snapshots from a previous run were valued against a different random walk
and are not comparable to new ones — plotting both on one line produces a meaningless cliff.
Truncating gives a chart that always describes a single continuous price regime. It is the
only table that is ever cleared; positions, cash, trades and the watchlist persist as
normal. Under Massive the table is left intact, because real prices *are* continuous across
a restart.

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON — trades executed, watchlist changes made; null for user messages)
- `created_at` TEXT (ISO timestamp)

The `actions` JSON is the contract between the backend and the chat panel. It records what
was actually attempted and what actually happened — not what the LLM said it would do:

```json
{
  "trades": [
    {"ticker": "AAPL", "side": "buy", "quantity": 10, "price": 190.24, "status": "filled"},
    {"ticker": "TSLA", "side": "buy", "quantity": 50, "status": "rejected",
     "error": "Insufficient cash: need $12,450.00, have $8,097.60"}
  ],
  "watchlist_changes": [
    {"ticker": "PYPL", "action": "add", "status": "ok"}
  ]
}
```

`status` is `"filled"` or `"rejected"` for trades, `"ok"` or `"rejected"` for watchlist
changes. `price` is present only on a filled trade. `error` is present only on a rejected
one. Empty arrays are omitted. The same object is returned inline by `POST /api/chat`.

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
| GET | `/api/portfolio/history` | Portfolio value snapshots over time (for P&L chart). Accepts `?since=<ISO timestamp>` and `?limit=<n>` (default: last 500 snapshots) |
| GET | `/api/trades` | Trade history, newest first, for the blotter. Accepts `?limit=<n>` (default: last 50) |

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
| GET | `/api/state` | Everything the frontend needs on load: portfolio, watchlist with prices, cash, recent trades. One call instead of four |
| GET | `/api/health` | Health check (for Docker/deployment) |

`GET /api/state` exists purely so the first paint needs a single round-trip. It returns the
composition of `/api/portfolio`, `/api/watchlist` and `/api/trades`; those endpoints remain
for refetching after a mutation.

### Trade Validation

`POST /api/portfolio/trade` applies exactly these rules. The same rules apply to trades the
AI executes — there is one code path, not two.

- `quantity` must be a number greater than 0. Fractional quantities are allowed.
- `side` must be `"buy"` or `"sell"`.
- The ticker must have a live price in the cache; a trade in an unpriced ticker is rejected.
- **Buy**: requires `quantity * price <= cash_balance`.
- **Sell**: requires `quantity <= position.quantity`. Selling a ticker with no position, or
  more than is held, is rejected.
- **No shorting and no margin.** A rejected trade returns HTTP 400 with an `error` string
  and changes no state.

Buying a ticker not on the watchlist is allowed; it joins the tracked ticker set (§6).

### Watchlist Rules

- Tickers are uppercased and trimmed on input. A symbol must be 1-5 characters, letters only.
- Adding a ticker already on the watchlist is a **no-op returning 200** with the current
  watchlist — idempotent, so a retry or a duplicate AI suggestion is harmless.
- The watchlist is capped at **30 tickers**. Adding past the cap returns 400. This bounds
  the Massive poll payload and stops the AI from adding symbols in a loop.
- Removing a ticker is allowed even while a position is open in it; the ticker leaves the
  watchlist panel but stays priced (§6).

---

## 9. LLM Integration

When writing code to make calls to LLMs, use the openrouter-inference skill to call LiteLLM
via OpenRouter with the free model `openrouter/nvidia/nemotron-3.5-lightning:free`. **Forced
tool calling** is used to obtain structured results — see below for why.

There is an OPENROUTER_API_KEY in the .env file in the project root.

### Why Tool Calling, Not Structured Outputs

The app must cost nothing to run, which means a model with the `:free` suffix. Free models
on OpenRouter do not advertise `response_format` among their supported parameters, so
Structured Outputs are unavailable — but `tools` and `tool_choice` are supported.

The backend therefore defines a single function, `submit_response`, whose parameter schema is
the response schema below, and forces the model to call it:

```python
tool_choice={"type": "function", "function": {"name": "submit_response"}}
```

The arguments come back as JSON that already matches the schema. They are still validated
with Pydantic before use — a forced tool call is reliable, not guaranteed, and an empty
`tool_calls` list must be handled rather than indexed into.

This was verified against the model with a spike before being written down: 18 calls across
three runs, of which forced tool calling produced a schema-valid response 8 times out of 9.
Prompt-based JSON was also tried and scored 6 out of 6, but was consistently slower in
head-to-head runs and needs extra parsing to strip code fences that a tool call never has.

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads the **last 20 messages** from the `chat_messages` table — a fixed window, so the prompt cannot grow without bound over a long session
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via LiteLLM → OpenRouter with a forced `submit_response` tool call, using the openrouter-inference skill
5. Validates the returned tool-call arguments against the Pydantic response model
6. Auto-executes any trades or watchlist changes specified in the response, collecting a per-action result (filled / rejected, with the fill price or the error)
7. Stores the message and the action results in `chat_messages` using the `actions` shape defined in §7
8. Returns the message plus the action results to the frontend in one complete JSON response (no token-by-token streaming — see below)

### Latency and the Progress Indicator

A free endpoint is heavily shared and has no latency guarantee. Measured against this model:
**7 to 58 seconds per response, median roughly 20-30 seconds.** That is the single biggest
change from a paid provider and it shapes the UI more than anything else in this section.

The response stays non-streaming. Adding token-by-token streaming would mean an SSE channel
for chat, partial-JSON handling, and a tool call that cannot be parsed until it is complete
anyway — real complexity for a first token that still arrives seconds late. Instead the wait
is made honest rather than hidden:

- The chat panel shows an **elapsed-time counter** from the moment the request is sent, so
  the user can see the app is working rather than frozen.
- Alongside it, an explicit expectation: *"Free model — this usually takes 20-30 seconds."*
  Stating the cost up front turns a broken-feeling wait into an understood one.
- After **60 seconds**, the message changes to *"Still working — free endpoints are
  sometimes slow."* The request is not cancelled.
- After **120 seconds**, the request is abandoned client-side and an error message offers a
  retry. The backend request keeps its own timeout at the same value.
- The input is disabled while a request is in flight, so a slow response cannot be
  compounded by a queue of impatient resends against a 20-per-minute rate limit.

### Rate Limits

Free models allow **20 requests per minute and 50 per day**, rising to 1000 per day once at
least 10 USD of credit has been purchased on the account. Two consequences: E2E tests must
run with `LLM_MOCK=true` rather than spend the daily budget, and a demo with several people
chatting at once can exhaust 50 requests quickly. The daily limit is a property of the
account, not of this app, and is not something the backend tries to manage — but a 429 from
OpenRouter must surface as a readable chat error, not a stack trace.

### Response Schema

The parameter schema of the forced `submit_response` tool call. The model's arguments arrive
as JSON matching this shape:

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

Defined once as a Pydantic model; `model_json_schema()` supplies the tool's `parameters` and
the same model validates the response. There is no second, hand-written copy of the schema.

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:
- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

**The message is an intent, not a receipt.** Execution happens after the LLM has already
written its reply, so the assistant may say "Buying 10 AAPL" for a trade that is then
rejected for insufficient cash. There is no second LLM call to reconcile this. Instead:

- The backend returns the action results alongside the message.
- The chat panel renders each action as its own chip beneath the message — green for filled
  (with ticker, quantity and fill price), red for rejected (with the error text).
- The chips, not the prose, are the authoritative record of what happened. A rejection is
  therefore always visible to the user even when the message text disagrees with it.

The next turn's context includes the previous turn's action results, so the assistant sees
the rejection and can respond to it if the user asks.

### System Prompt Guidance

The LLM should be prompted as "FinAlly, an AI trading assistant" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven in responses
- Always answer by calling `submit_response`; never reply with plain text

Keep the system prompt short. Every token of it is re-sent on each turn against a shared free
endpoint, and prompt length is one of the few levers on the latency described above.

### LLM Mock Mode

When `LLM_MOCK=true`, the backend returns deterministic mock responses instead of calling OpenRouter. This enables:
- Fast, free, reproducible E2E tests
- Development without an API key
- CI/CD pipelines

Mock responses are **keyword-matched on the user's message**, not a single canned reply —
the E2E suite needs the chat to actually execute a trade. The rules, in order:

| User message contains | Mock response |
|---|---|
| `buy <n> <TICKER>` | `message` confirming the buy, plus that trade in `trades` |
| `sell <n> <TICKER>` | `message` confirming the sell, plus that trade in `trades` |
| `watch <TICKER>` / `add <TICKER>` | `message` confirming, plus a `watchlist_changes` add |
| anything else | A fixed portfolio-summary message, no actions |

The mock returns the response object directly, replacing the network call but not the
validation or execution that follows it. The response flows through the same auto-execution
path as a real one, so an E2E test can assert on a genuine rejection by mocking a buy the
cash balance cannot cover. Mock responses are instant, so the progress indicator never
appears in E2E runs — it needs its own frontend unit test with a delayed promise.

---

## 10. Frontend Design

### Layout

The frontend is a single-page application with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), daily change % (from `prev_close`, see §6), and a sparkline mini-chart (accumulated from SSE since page load)
- **Main chart area** — larger chart for the currently selected ticker, with at minimum price over time. Clicking a ticker in the watchlist selects it here. Like the sparklines, it plots prices accumulated client-side since page load, so it begins empty and fills in — show a "collecting data" placeholder until roughly ten points exist.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss)
- **P&L chart** — line chart showing total portfolio value over time, using data from `portfolio_snapshots`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade blotter** — scrolling log of executed trades, newest first: time, ticker, side (green BUY / red SELL), quantity, fill price, notional value. Fed by `GET /api/trades` and refetched after every trade, manual or AI-executed. Read-only — trades cannot be cancelled or amended.
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill. A rejected trade (§8) surfaces its error string beside the bar.
- **AI chat panel** — docked/collapsible sidebar. Message input, scrolling conversation history, and the progress indicator described below while waiting for the LLM. Each message's executed actions render as green (filled) or red (rejected) chips beneath the message text, per §9.
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Technical Notes

- On load, call `GET /api/state` once for portfolio, watchlist, cash and recent trades, then open the SSE connection. Refetch `/api/portfolio` and `/api/trades` after any mutation (trade, chat action).
- Use `EventSource` for SSE connection to `/api/stream/prices`
- **Recharts** for every chart — sparklines, main price chart, P&L line, and the portfolio treemap. It is the only one of the candidates that covers all four, so the app needs a single charting dependency rather than two.
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- **Header total value is computed client-side** — cash from the last `/api/portfolio` response, position values from the live SSE prices. Do not poll `/api/portfolio` to keep the header current; nothing polls at the SSE cadence.
- **Connection dot**: track the timestamp of the last SSE message. Green if it is under 10 seconds old, red otherwise. Two states only (§2).
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed
- Tailwind CSS for styling with a custom dark theme

### Chat Progress Indicator

The free model takes 7-58 seconds to answer (§9). A bare spinner over that span reads as a
hung app, so the panel states the cost of being free instead of hiding it. A placeholder
assistant bubble appears the moment the message is sent and passes through four stages:

| Elapsed | What the user sees |
|---|---|
| 0s | Animated dots, an elapsed-second counter, and the line *"Free model — this usually takes 20-30 seconds."* |
| 60s | Counter continues; the line becomes *"Still working — free endpoints are sometimes slow."* |
| 120s | Request abandoned. The bubble becomes an error with a **Retry** button that resends the same message. |
| Response | The bubble is replaced by the real message and its action chips. |

Requirements:

- The elapsed counter must tick visibly from the first second. It is the only honest signal
  that something is still happening, and it costs one `setInterval`.
- The message input is **disabled while a request is in flight**. Without this, an impatient
  user resends and burns the 20-requests-per-minute budget on answers they will discard.
- The user's own message appears in the history immediately, not after the response arrives.
- Never show a fake progress bar. There is no progress to report — an elapsed counter is
  truthful, a bar that fills at a guessed rate is not.
- A 429 from OpenRouter renders as a readable chat error naming the rate limit, not a
  generic failure.

### Client-Side Price History

The frontend keeps a bounded in-memory buffer per tracked ticker (the last ~300 points,
roughly 2.5 minutes at the 500ms cadence) fed from the SSE stream. Sparklines and the main
chart both read from it. It is deliberately not persisted — a page refresh starts it over,
and that is the accepted trade for not storing tick history server-side.

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

### Database Persistence

The SQLite database persists via a **bind mount** of the project's `db/` directory:

```bash
docker run -v "$PWD/db:/app/db" -p 8000:8000 --env-file .env finally
```

The backend writes `finally.db` to `/app/db`, which is the host's `db/finally.db`. A bind
mount rather than a named volume, so the database file is visible and inspectable on the
host — worth more in a teaching project than the portability a named volume would buy.
Deleting `db/finally.db` resets the app to seed state.

On Windows the start script uses `${PWD}` PowerShell-style; the mount is otherwise identical.

### Local Development (without Docker)

Docker is for running the finished app. Day-to-day development runs the two halves
separately:

```bash
# terminal 1
cd backend && uv run uvicorn app.main:app --reload --port 8000

# terminal 2
cd frontend && npm run dev          # serves on :3000
```

`next dev` on :3000 is a different origin from uvicorn on :8000, which would reintroduce the
CORS problem the production build avoids. Rather than enabling CORS on the backend, the
frontend proxies in dev via `next.config.js`:

```js
async rewrites() {
  return [{ source: '/api/:path*', destination: 'http://localhost:8000/api/:path*' }];
}
```

Frontend code therefore always calls relative `/api/*` paths, identical in dev and in
production, and the backend never needs CORS middleware. The rewrite is inert in a static
export build.

### Start/Stop Scripts

**`scripts/start_mac.sh`** (macOS/Linux):
- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container with the volume mount, port mapping, and `.env` file
- Prints the URL to access the app
- Optionally opens the browser

**`scripts/stop_mac.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT touch `db/` (data persists)

**`scripts/start_windows.ps1`** / **`scripts/stop_windows.ps1`**: PowerShell equivalents for Windows.

All scripts should be idempotent — safe to run multiple times.

These four scripts are the only supported way to run the container. There is no
`docker-compose.yml` for production — a single container needs no orchestration, and one
launch path is easier to document than two. `test/docker-compose.test.yml` exists solely to
pair the app container with a Playwright container (§12).

### Optional Cloud Deployment

The container is designed to deploy to AWS App Runner, Render, or any container platform. A Terraform configuration for App Runner may be provided in a `deploy/` directory as a stretch goal, but is not part of the core build.

---

## 12. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**:
- Market data: simulator generates valid prices, GBM math is correct, Massive API response parsing works, both implementations conform to the abstract interface, an unseeded ticker gets a fallback seed price, `change_percent_from_close` is computed against `prev_close`
- Portfolio: trade execution logic, P&L calculations, edge cases (selling more than owned, buying with insufficient cash, selling at a loss, a fully-sold position row is deleted, quantity <= 0 is rejected)
- Tracked ticker set: removing a watchlist ticker with an open position keeps it priced; removing one without a position stops pricing it
- LLM: tool-call arguments validate against the Pydantic model, an empty `tool_calls` list is handled rather than indexed into, malformed arguments fail gracefully, trade validation within chat flow, a rejected trade still produces a well-formed `actions` object, a 429 surfaces as a readable error
- API routes: correct status codes, response shapes, error handling, watchlist idempotency and the 30-ticker cap, `/api/trades` ordering (newest first) and `limit` handling
- Blotter integrity: a rejected trade writes no row; an AI-executed trade writes one indistinguishable from a manual trade
- Startup: simulator mode truncates `portfolio_snapshots` and leaves positions, cash, trades and watchlist untouched; Massive mode leaves snapshots intact

**Frontend (React Testing Library or similar)**:
- Component rendering with mock data
- Price flash animation triggers correctly on price changes
- Watchlist CRUD operations
- Portfolio display calculations
- Chat message rendering, and the progress indicator's four stages driven by a delayed promise: counter ticks, the 60s text change, the 120s abandon with a working Retry, and the input staying disabled throughout

### E2E Tests (in `test/`)

**Infrastructure**: A separate `docker-compose.test.yml` in `test/` that spins up the app container plus a Playwright container. This keeps browser dependencies out of the production image.

**Environment**: Tests run with `LLM_MOCK=true` by default for speed and determinism.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming, connection dot is green
- Add and remove a ticker from the watchlist, including a ticker with no seed price
- Buy shares: cash decreases, position appears, portfolio updates
- Sell part of a position: cash increases, quantity decreases
- Sell all of a position: the row disappears from the positions table
- Rejected trade: buy more than the cash balance allows, assert the error is shown, nothing changed, and no blotter row was written
- Trade blotter: a buy then a sell appear newest-first with correct side, quantity and fill price
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): "buy 5 AAPL" produces a green filled chip and a real position; "buy 1000 AAPL" produces a red rejected chip and no position
- SSE resilience: disconnect and verify reconnection, and that the connection dot goes red then green

---

## 13. Decisions Log

A documentation review raised 25 questions and gaps. All are now resolved in the body of
this document above; this section records what was decided and why, so the reasoning is not
lost and the same questions are not reopened.

### Resolved

| # | Question | Decision | Where |
|---|---|---|---|
| 1 | Daily change % had no baseline — the cache held only the previous *tick* | Each ticker carries a `prev_close`: from `seed_prices.py` in the simulator, from the API field under Massive. `PriceUpdate` gains `change_from_close` | §6, §10 |
| 2 | Main detail chart had no data source | Client-side accumulation from SSE, same as sparklines. No server-side tick history, no new table, no new endpoint. Shows a "collecting data" placeholder until ~10 points exist | §2, §10 |
| 3 | Unknown tickers (§9's own example adds `PYPL`, which has no seed) | Fallback seed — random $50-$500, default drift/volatility — rather than a symbol whitelist. Watchlist routes must also call `source.add_ticker()`; a database write alone does not start pricing | §6, §8 |
| 4 | Chat message was written before trades executed, so it could claim a fill that was rejected | The message is an intent, not a receipt. Action results return alongside it and render as green/red chips, which are authoritative. No second LLM call | §7, §9, §10 |
| 5 | §11 showed a named volume, §4 described a bind mount | Bind mount `./db:/app/db`, so the database file is visible on the host | §4, §11 |
| 6 | Priced tickers were said to equal the watchlist | Tracked set is watchlist ∪ tickers with an open position. A de-watchlisted holding stays priced | §6 |
| 7 | Realized P&L had no home in the schema | No `realized_pnl` column. Sale proceeds land in `cash_balance` and total value already reflects them | §7 |
| 8 | Position lifecycle on a full sell was "updates or disappears" | The row is deleted. A row in `positions` always means an open holding | §7, §12 |
| 9 | Shorting and negative quantities were never ruled out | Explicit validation rules: quantity > 0, buy needs cash, sell needs shares, no shorting, no margin, one code path shared with the AI | §8 |
| 10 | `portfolio_snapshots` grew unbounded; history endpoint took no parameters | 60s cadence instead of 30s, `?since=` and `?limit=` (default last 500), and the task skips a tick if a held ticker has no price yet | §7, §8 |
| 11 | "Recent conversation history" was unbounded | Last 20 messages | §9 |
| 12 | `chat_messages.actions` JSON shape was undefined | Fully specified with `status` / `price` / `error` fields; same object returned by `POST /api/chat` | §7 |
| 13 | Nothing read the `trades` table | `GET /api/trades` added, plus a trade blotter panel showing executed trades newest-first | §7, §8, §10 |
| 14 | `.env` location was contradictory between §5 and §11 | Docker passes `--env-file`; locally `load_dotenv(Path(__file__).parents[2] / ".env")`. Both end at `os.environ` | §5 |
| 15 | No local development story; `next dev` would reintroduce CORS | `rewrites()` proxy in `next.config.js`, so frontend code always calls relative `/api/*` and the backend never needs CORS middleware | §11 |
| 16 | Massive free-tier call budget was unstated | One grouped request per poll covering all tickers — 4 calls/min against a 5/min limit regardless of ticker count | §6 |
| 17 | Simulator resets to seed prices on restart, causing a P&L cliff | `portfolio_snapshots` is truncated on startup in simulator mode, so the chart always covers one continuous price regime. Positions, cash, trades and watchlist persist. Left intact under Massive | §6, §7 |
| 18 | "Red = disconnected" was unreachable, since `EventSource` retries forever | Two states: green if a message arrived in the last 10s, red otherwise | §2, §10 |
| 19 | Header total value — client-computed or polled? | Client-computed from SSE prices plus the last known cash. Nothing polls at the SSE cadence | §10 |
| 20 | Ticker normalization was unspecified | Uppercased, trimmed, 1-5 letters. A duplicate add is an idempotent 200 | §8 |
| 21 | The AI could add tickers without limit | Watchlist capped at 30; also bounds the Massive poll payload | §8 |
| 22 | Stale prices under Massive (15s, frozen out of hours) | Accepted, not guarded. No staleness check, no market-hours logic | §6 |
| 23 | `LLM_MOCK` responses were "deterministic" but unspecified | Keyword-matched on the user's message with a documented rule table, so E2E tests can drive real fills *and* real rejections | §9 |
| 24 | "Lightweight Charts **or** Recharts" left the choice open | Recharts for all four chart types. It is the only candidate that covers the treemap, so the app carries one charting dependency instead of two | §10 |
| 25 | `backend/db/` and `/db` were two directories named `db` | Renamed to `backend/app/database/`, alongside `backend/app/market/` | §4 |

### Switched to a free LLM (later change)

Cerebras was dropped because it costs money; the app must run for free. This was not part of
the original review, but it changed enough of §9 to belong in the same log.

| # | Question | Decision | Where |
|---|---|---|---|
| 26 | Cerebras is a paid inference provider | Removed. No provider pinning at all — `extra_body={"provider": ...}` is exactly what routes a request to a paid provider | §3, §9 |
| 27 | The requested model `nvidia/nemotron-3-ultra-550b-a55b:free` does not exist on OpenRouter | Verified against the live model list and replaced with `nvidia/nemotron-3.5-lightning:free` | §5, §9 |
| 28 | Free models do not support `response_format`, which §9 depended on | Forced tool calling via `tools` + `tool_choice`, which free models do support. Verified by spike: 8/9 schema-valid calls, against 6/6 for prompt-based JSON but consistently slower and needing fence-stripping | §9 |
| 29 | "Cerebras is fast enough that a loading indicator is sufficient" no longer holds — measured 7-58s | Response stays non-streaming, but the wait is made honest: elapsed counter, a stated 20-30s expectation, new text at 60s, abandon with Retry at 120s, input disabled throughout | §9, §10, §12 |
| 30 | Free models are rate-limited to 20/min and 50/day | Documented. E2E runs on `LLM_MOCK=true`; a 429 must render as a readable chat error | §9 |
| 31 | The model was hardcoded in the plan | `OPENROUTER_MODEL` environment variable, defaulting to the free model, so swapping models needs no code change | §5 |
| 32 | Environment variable names are case-sensitive in the Linux container | Documented — a lowercase `openrouter_api_key` works on Windows but fails in Docker | §5 |
| 33 | The `cerebras-inference` skill named a provider that is no longer used | Renamed to `openrouter-inference`, with the tool-calling snippet, the latency warning and the rate limits | — |

### Also simplified

- **`docker-compose.yml` dropped.** Four start/stop scripts, a compose wrapper, and a test
  compose file were three ways to launch one container. The scripts are the only supported
  path; `test/docker-compose.test.yml` remains for pairing the app with Playwright.
- **`GET /api/state` added.** First paint needed 2-3 round-trips for portfolio, watchlist and
  cash; now one. The individual endpoints remain for refetching after a mutation.

### Deliberately left alone

- **`user_id` on every table.** Speculative for a single-user app, but §7 justifies it, it
  costs nothing, and it keeps the shared-price-cache design in §6 honest.
- **Market orders only.** Well chosen; the rationale table in §3 earns its place.

### Open questions

None. Every item raised by the review is resolved in the body above. New questions should be
appended here as they come up, and moved into the table once decided.
