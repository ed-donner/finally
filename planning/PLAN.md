# FinAlly — AI Trading Workstation

> **Product specification and cross-agent contract.** Build-process instructions for coding agents live in [`AGENTS.md`](AGENTS.md). The HTTP contract (request/response shapes, status codes) lives in [`API.md`](API.md). Section 13 records how the 2026-11-18 review items were resolved.

---

## 1. Vision

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

It is the capstone project for an agentic AI coding course and is built entirely by coding agents.

## 2. User Experience

### First Launch

The user runs a single start script (or the equivalent Docker command). The script builds the image if needed, starts the container, and opens a browser to `http://localhost:8000` unless `--no-browser` is passed. No login, no signup. The user immediately sees:

- A watchlist of 10 default tickers with live-updating prices in a grid
- $10,000 in virtual cash
- A dark, data-rich trading terminal aesthetic
- An AI chat panel ready to assist

### What the User Can Do

- **Watch prices stream** — prices flash green (uptick) or red (downtick) with CSS animations that fade over ~500 ms
- **View sparkline mini-charts** — price action beside each ticker, populated from the stream snapshot on load and then accumulated live
- **Click a ticker** to see a larger chart with price history in the main chart area
- **Buy and sell shares** — market orders only, instant fill at the current price, no fees, no confirmation dialog
- **Monitor their portfolio** — a treemap heatmap of positions plus a P&L chart of total portfolio value over time
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, % change
- **Chat with the AI assistant** — ask about the portfolio, get analysis, and have the AI execute trades and manage the watchlist through natural language
- **Manage the watchlist** — add/remove tickers manually or via the AI chat
- **Reset the demo** — restore the initial state (cash, positions, trades, watchlist, chat, history) from the header, behind a confirmation

### Non-Goals

These are explicitly out of scope. Agents should not add them.

- Authentication, accounts, multi-user sessions (the `user_id` columns exist only for future use). The app is not designed for exposure beyond localhost.
- Limit, stop, or any order type other than market orders
- Commissions, fees, slippage, or partial fills
- Short selling, margin, options, futures, crypto, or fractional-share *prices*
- Realized P&L reporting and tax lots (average-cost P&L only)
- Real brokerage connectivity of any kind
- Token-by-token LLM streaming
- Mobile-first layout (tablet is functional; desktop is the target)

### Visual Design

- **Dark theme**: backgrounds around `#0d1117` or `#1a1a2e`, muted gray borders, no pure black
- **Price flash animations**: brief green/red background highlight on change, fading over ~500 ms via CSS transitions
- **Connection status indicator**: a small colored dot in the header (see Section 10 for the state rules)
- **Professional, data-dense layout**: inspired by Bloomberg/trading terminals — every pixel earns its place
- **Responsive but desktop-first**: optimized for ≥1280 px wide screens, functional at ≥768 px

### Color Scheme

| Role | Value | Used for |
|---|---|---|
| Accent Yellow | `#ecad0a` | Accents, Sell button, highlights |
| Blue Primary | `#209dd7` | Links, Buy button, selected ticker |
| Purple Secondary | `#753991` | Submit buttons (chat send, watchlist add) |
| Gain | `#3fb950` | Positive P&L, uptick flash, connected dot |
| Loss | `#f85149` | Negative P&L, downtick flash |

Green and red are reserved for P&L and price movement. They are never used for actions.

## 3. Architecture Overview

### Single Container, Single Port

```
┌─────────────────────────────────────────────────┐
│  Docker Container (port 8000)                   │
│                                                 │
│  FastAPI (Python/uv), 1 uvicorn worker          │
│  ├── /api/*          REST endpoints             │
│  ├── /api/stream/*   SSE streaming              │
│  └── /*              Static file serving        │
│                      (Next.js export, SPA)      │
│                                                 │
│  SQLite database (bind-mounted at db/)          │
│  Background tasks: market data, snapshots       │
└─────────────────────────────────────────────────┘
```

- **Frontend**: Next.js with TypeScript, static export (`output: 'export'`), served by FastAPI. Unknown non-`/api` paths fall back to `index.html`.
- **Backend**: FastAPI (Python 3.12), managed as a `uv` project. **Must run as a single worker**: the price cache, the trade lock, and the SSE broadcaster are in-process state.
- **Database**: SQLite, single file at `db/finally.db`, bind-mounted into the container.
- **Real-time data**: Server-Sent Events (SSE), one-way server→client push.
- **AI integration**: LiteLLM → OpenRouter (Cerebras as the inference provider), structured outputs for trade execution.
- **Market data**: environment-variable driven — simulator by default, Massive API if a key is provided.

### Why These Choices

| Decision | Rationale |
|---|---|
| SSE over WebSockets, static export | One-way push is all we need. One origin, one port, no CORS, one container. |
| SQLite over Postgres | Zero configuration, a single transactional file, and ample for a single-user app. The `user_id` columns keep the door open for multi-user later. |
| Market orders only | Eliminates order books, limit logic, and partial fills, so portfolio math stays simple and testable. |

Tooling choices (uv, Next.js, FastAPI, Tailwind) follow from the course curriculum and are not repeated here.

---

## 4. Directory Structure

```
finally/
├── frontend/                  # Next.js + TypeScript project (self-contained, static export)
├── backend/                   # FastAPI uv project (self-contained, own pyproject.toml)
│   ├── app/
│   │   ├── market/            # Simulator, Massive client, price cache, SSE broadcaster
│   │   ├── store/             # schema.sql, seed data, repository layer, lazy init
│   │   ├── portfolio/         # Trade execution, P&L, snapshots
│   │   ├── chat/              # LLM client, prompt builder, response validation
│   │   └── api/               # Route modules
│   └── tests/
├── planning/                  # Shared contract for agents
│   ├── PLAN.md                # This document (product)
│   ├── API.md                 # HTTP and SSE contract
│   └── AGENTS.md              # Build-process instructions
├── scripts/
│   ├── start_mac.sh           # Build if needed, run container, open browser
│   ├── stop_mac.sh            # Stop and remove container (volume/data kept)
│   ├── start_windows.ps1      # PowerShell equivalent
│   └── stop_windows.ps1       # PowerShell equivalent
├── test/                      # Playwright E2E tests + docker-compose.test.yml
├── db/                        # Runtime bind mount; finally.db lives here (gitignored)
│   └── .gitkeep
├── Dockerfile                 # Multi-stage build (Node → Python)
├── docker-compose.yml         # Optional convenience wrapper (dev only)
├── .env                       # Real secrets (gitignored)
├── .env.example               # Committed template with comments
├── .dockerignore              # Excludes .env, db/, node_modules, .git
└── .gitignore
```

### Key Boundaries

- **`frontend/`** knows nothing about Python. It talks only to `/api/*` and `/api/stream/*`. Internal structure is up to the frontend agent.
- **`backend/`** owns all server logic: store and initialization, routes, SSE, market data, portfolio logic, and LLM integration. Internal structure is up to the backend agent, within the module boundaries above.
- **`backend/app/store/`** holds the schema SQL, seed data, and lazy initialization. It is deliberately not named `db/`, to avoid confusion with the runtime `db/` directory at the root.
- **`planning/`** is the shared contract. If code and `planning/` disagree, the contract wins and the code is fixed (or the contract is changed first, per `AGENTS.md`).
- **`test/`** holds E2E tests. Unit tests live in `frontend/` and `backend/` under each framework's conventions.

---

## 5. Environment Variables

`.env.example` (committed):

```bash
# Required for chat. Without it the app runs, and /api/chat returns 503 llm_unavailable.
OPENROUTER_API_KEY=

# Optional. If empty or absent, the built-in market simulator is used.
# If set, prices come from the Massive API (formerly Polygon.io).
MASSIVE_API_KEY=

# Optional. Seconds between Massive polls. Free tier allows 5 calls/min, so keep >= 12.
MASSIVE_POLL_INTERVAL_SECONDS=15

# Optional. Massive calls allowed per minute, across all Massive requests (baseline,
# polls, and ticker validation). Set this to your plan's limit. Free tier: 5.
MASSIVE_CALLS_PER_MINUTE=5

# Optional. "true" returns deterministic mock LLM responses (E2E tests, offline dev).
LLM_MOCK=false
```

### Behavior

- `MASSIVE_API_KEY` set and non-empty → Massive client; otherwise → simulator.
- `LLM_MOCK=true` → mock LLM; no network call to OpenRouter.
- **Configuration source:** in the container, environment variables come only from `docker run --env-file .env`. The image does not contain `.env` (see `.dockerignore`). For local non-container development, the backend calls `load_dotenv()` on the project-root `.env` if it exists. This is the only place the backend reads the file.
- Values that are not secrets, such as the Massive base URL and the simulator's parameters, are constants in code. They are not environment variables.

---

## 6. Market Data

### Two Implementations, One Interface

Both the simulator and the Massive client implement one abstract `PriceSource` interface:

- `async start()` / `async stop()`
- `async add_ticker(symbol) -> None` — raises `UnknownTicker` if the source cannot price it
- `async remove_ticker(symbol) -> None`
- `tick() -> list[PriceUpdate]` — the updates produced since the last call

Everything downstream (price cache, SSE, portfolio valuation) depends only on this interface.

### Price Cache (shared, in-memory)

A single background task owns the cache. Per ticker it stores:

- `price` (latest), `previous_price` (the value before the most recent update, whatever the cadence), `timestamp`
- `prev_close` (baseline for daily change, see below). `null` until known.
- A rolling history of the last **600** `(timestamp_ms, price)` points. The buffer is in memory only; it is rebuilt from live ticks after a restart.
- Last price and `prev_close` per ticker are also persisted to `price_state` every 30 s, so a restart can resume from them (Section 7).

The cache covers the union of the watchlist and tickers with a non-zero position, so that held-but-unwatched tickers keep pricing.

**Daily change baseline:** `prev_close`. Massive: the previous session's close, fetched once per ticker per day. Simulator: the starting price, so the day change starts at 0% and drifts from there. `change_pct = (price − prev_close) / prev_close`.

### Simulator (Default)

- **Process:** per tick, each ticker's log-return follows GBM: `ln(S') − ln(S) = (μ − σ²/2)·Δt + σ·√Δt·z`.
- **Time scale:** a tick (≈500 ms wall-clock) advances market time by `Δt` equal to 30 trading seconds. With σ = 25%, a typical tick moves a ticker by about 0.06%, which flickers visibly without drifting far in a session. The constant is `SIM_TICK_MARKET_SECONDS = 30` in code.
- **Correlation (one market factor):** per tick, draw one market shock `z_m ~ N(0,1)`. Each ticker gets `z_i = β_i·z_m + √(1 − β_i²)·ε_i`, with `ε_i ~ N(0,1)` independent. The sample correlation between tickers *i* and *j* is therefore ≈ `β_i·β_j`. Betas are seed-table values in code: about 0.7 for tech names, 0.6 for financials, 0.5 default.
- **Random events (drama):** each ticker, each tick, has probability **1/1200** of a jump (≈ once per 10 minutes of wall-clock time per ticker). The jump is multiplicative, `S' = S·(1 ± J)`, with `J` uniform in [2%, 5%] and a random sign, applied after the diffusion step on that tick. Jumps are an injectable component with `jump_prob` (default 1/1200) and can be disabled with `SIM_EVENTS_ENABLED=false` in tests.
- **Effective volatility is higher than σ when jumps are on.** Jumps add roughly 2–3× the diffusion variance, so the realized annualized volatility is about 50% on screen, not 25%. This is deliberate for demo drama. The statistical tests (Section 12) run with jumps off, so σ can be verified exactly. If the product owner wants σ to be the observed volatility, lower `J` to 1–3% or lower `jump_prob`. That is a product decision, not a build decision.
- **Seeds:** realistic starting prices and per-ticker (μ, σ, β) live in `backend/app/market/seed_prices.py`. The spec does not pin values.
- **Unknown tickers:** the simulator accepts any symbol matching the format rule (Section 8). Unknown symbols get μ = 5%, σ = 25%, β = 0.5, and a starting price of $100.00.
- **Cadence:** emits a batch of updates every 500 ms.
- **Seeded:** the RNG takes a seed from `SIM_SEED` (constant in code, overridable in tests) so tests and demos are reproducible.

### Massive API (Optional)

- **REST polling, not WebSocket.** Works on all tiers.
- **One call shape for prices:** one batched request per poll for all cached tickers, using the multi-ticker snapshot endpoint (`GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=A,B,...`). Chunk at 250 symbols per request. *Spike S1 (Section 13.5) confirms the parameter name, the per-request limit, and the plan tier that serves it.*
- **Rate limiter (required):** every Massive request, including the baseline, polls, and validation, passes through one token-bucket limiter sized by `MASSIVE_CALLS_PER_MINUTE` (default 5). Requests that cannot be sent yet are queued in order. Price polls have priority over baseline lookups.
- **Baseline (`prev_close`):** if S1 confirms that the snapshot payload includes the previous-day close (`prevDay.c`), take `prev_close` from the snapshot and make no extra calls. Otherwise, call `GET /v2/aggs/ticker/{symbol}/prev` once per ticker per day, through the limiter, lowest priority. Until the baseline is known, the ticker's `prev_close` and `change_pct` are `null`. Startup does not block on baselines.
- **Interval:** `MASSIVE_POLL_INTERVAL_SECONDS`, default 15. If the watchlist plus held tickers would need more than the limit allows at that interval, the poll interval is stretched automatically and a warning is logged.
- **Retry rule:** on HTTP 429 or 5xx, keep the last known prices, log a warning, and try again at the next interval. Never block SSE on the poller.
- **Degraded state:** after 3 consecutive failed polls, `source_status` becomes `degraded` in the `heartbeat` event (API.md). The connection indicator shows yellow (Section 10). It returns to `ok` after one successful poll.
- **Unknown symbol:** an empty result is treated as not-found, whether the endpoint returns 404 or 200 with zero results. `add_ticker` then raises `UnknownTicker`. S1 confirms which response occurs, and the rule covers both.
- **Base URL and plan tier:** pinned as a constant in the market client, not an environment variable. S1 confirms the current base URL. If the free tier does not include the snapshot endpoint, `.env.example` and this section say so, and Massive mode requires a paid tier.
- Flashes are infrequent in this mode, since prices change only on poll. That is expected, not a bug.

### SSE Streaming

- **Endpoint:** `GET /api/stream/prices` — `text/event-stream`, one long-lived connection. The client uses native `EventSource`.
- **On connect:** the server sends one `snapshot` event with the current price, `prev_close`, and the 600-point history for every cached ticker. The client replaces its entire state with it. This gives first paint, sparkline and chart history, and a clean recovery after reconnect with no gaps.
- **Per tick:** one `prices` event containing an array of only the tickers that changed since the previous event.
- **Heartbeat:** a named `heartbeat` event every 5 s, always, carrying `source_status`. Browsers do not expose SSE comment lines to page code, so comments cannot signal liveness. The heartbeat is also how the connection indicator learns that the upstream source is degraded.
- **Retry hint:** `retry: 1000` is sent on connect.
- **Reconnection:** handled by `EventSource`. The server is stateless per connection; each reconnect gets a fresh `snapshot`.
- **Wire format and field definitions:** see [`API.md`](API.md#sse-price-stream). That file is the contract; this section describes the behavior.

---

## 7. Database

### Lazy Initialization

On application startup (FastAPI lifespan, before serving requests), the backend:

1. Opens `db/finally.db`, creating it if absent.
2. Creates any missing tables from `backend/app/store/schema.sql`.
3. If `users_profile` is empty, seeds the default user, the watchlist, and one portfolio snapshot at $10,000.

No separate migration step and no manual setup. A fresh volume starts clean and seeded. The `/api/health` endpoint reports `db: ok` only after initialization completes.

SQLite runs in the default rollback-journal mode (not WAL). This avoids `-wal`/`-shm` side files on bind mounts that cross the Docker Desktop file-sharing boundary.

### Schema

All tables carry `user_id TEXT NOT NULL DEFAULT 'default'`. Single-user today; the column is in place for multi-user later without a migration.

**Money and quantity storage:** all cash and cost amounts are stored as INTEGER **cents**. All quantities are stored as INTEGER **ten-thousandths** of a share (`quantity_e4`, so `1.5` shares is `15000`). Conversion happens at the API boundary, so REST and SSE still use decimals. Integer storage removes binary floating-point drift from cash and quantity arithmetic, and the tolerance rules in Section 8 become exact comparisons.

**users_profile** — one row, always (invariant)
- `id` TEXT PRIMARY KEY (`"default"`)
- `cash_cents` INTEGER NOT NULL (default `1000000`, which is $10,000.00)
- `created_at` TEXT (ISO 8601 UTC)

**watchlist**
- `user_id` TEXT NOT NULL DEFAULT `'default'`
- `ticker` TEXT NOT NULL
- `added_at` TEXT NOT NULL
- PRIMARY KEY `(user_id, ticker)`

**positions** — cache of current holdings, one row per ticker per user
- `user_id` TEXT NOT NULL DEFAULT `'default'`
- `ticker` TEXT NOT NULL
- `quantity_e4` INTEGER NOT NULL CHECK (`quantity_e4 > 0`)
- `avg_cost` REAL NOT NULL (display and P&L only; never used for cash arithmetic)
- `updated_at` TEXT NOT NULL
- PRIMARY KEY `(user_id, ticker)`

**trades** — append-only, **authoritative** record of all executions
- `id` INTEGER PRIMARY KEY AUTOINCREMENT
- `user_id` TEXT NOT NULL DEFAULT `'default'`
- `ticker` TEXT NOT NULL
- `side` TEXT NOT NULL CHECK (`side IN ('buy','sell')`)
- `quantity_e4` INTEGER NOT NULL CHECK (`quantity_e4 > 0`)
- `price` REAL NOT NULL CHECK (`price > 0`) (the fill price, as executed)
- `amount_cents` INTEGER NOT NULL CHECK (`amount_cents >= 0`) (the cash moved, computed once and stored, so the invariant can be replayed exactly)
- `executed_at` TEXT NOT NULL
- INDEX `(user_id, executed_at)`

**portfolio_snapshots** — total value over time for the P&L chart
- `id` INTEGER PRIMARY KEY AUTOINCREMENT
- `user_id` TEXT NOT NULL DEFAULT `'default'`
- `total_value_cents` INTEGER NOT NULL
- `recorded_at` TEXT NOT NULL
- INDEX `(user_id, recorded_at)`
- Written every 30 s by a background task, immediately after each trade, and once at first initialization.
- **Retention:** rows older than 7 days are pruned by the snapshot task (about 20,000 rows at steady state).

**chat_messages** — conversation history
- `id` INTEGER PRIMARY KEY AUTOINCREMENT
- `user_id` TEXT NOT NULL DEFAULT `'default'`
- `role` TEXT NOT NULL CHECK (`role IN ('user','assistant')`)
- `content` TEXT NOT NULL
- `actions` TEXT (JSON; for assistant messages, the same object the chat API returns, minus `message`; NULL for user messages)
- `created_at` TEXT NOT NULL

**price_state** — last known price per ticker, for restart recovery
- `ticker` TEXT PRIMARY KEY
- `price` REAL NOT NULL
- `prev_close` REAL (nullable)
- `updated_at` TEXT NOT NULL
- Written every 30 s by the snapshot task, and on clean shutdown. Read once at startup (Section 6, restart rules).

### Restart Rules

- **Simulator:** each ticker that has a `price_state` row starts from that price instead of the seed price. Tickers without a row start from the seed. This keeps unrealized P&L continuous across restarts (H8).
- **Massive:** held tickers with a `price_state` row are valued at that stored price until the first successful poll, and the response marks them `stale: true`. Held tickers with no stored price are valued as `null` (API.md portfolio rules).
- **Snapshots:** the snapshot task values every held ticker at its current or stored price. A ticker with no price at all causes the snapshot to be skipped and logged, never recorded as zero.

### Invariants

These are tested (Section 12) and must hold after every operation:

1. `users_profile` has exactly one row.
2. `cash_cents = 1000000 − Σ(amount_cents of buys) + Σ(amount_cents of sells)`, exact integer equality, where `amount_cents` is stored per trade.
3. `positions` can be rebuilt exactly from `trades` by replaying them in order (integer `quantity_e4`, exact). `trades` is the source of truth; `positions` is a cache updated in the same transaction as each trade.
4. No position row has `quantity_e4 ≤ 0`.

### Default Seed Data

- One user profile: `id="default"`, `cash_cents=1000000`
- Ten watchlist entries: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX
- One portfolio snapshot: `total_value_cents=1000000`
- No positions, trades, or chat messages

Reset (Section 8) returns the database to exactly this state.

---

## 8. API Endpoints

The full contract, with request and response bodies, status codes, and the error format, is in [`API.md`](API.md). This section summarizes it and records the business rules.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Liveness and readiness (DB, market source, LLM configured) |
| GET | `/api/stream/prices` | SSE: `snapshot` on connect, then `prices` per tick |
| GET | `/api/portfolio` | Cash, positions with P&L, total value |
| POST | `/api/portfolio/trade` | Execute a market order: `{ticker, quantity, side}` |
| GET | `/api/portfolio/history` | Snapshots for the P&L chart (last 7 days) |
| POST | `/api/portfolio/reset` | Restore seed state. Requires `{"confirm": "RESET"}` |
| GET | `/api/watchlist` | Watchlist tickers (no prices; prices come from SSE) |
| POST | `/api/watchlist` | Add a ticker: `{ticker}` |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker (a held position keeps pricing) |
| GET | `/api/chat/messages` | Recent chat history for first paint (`?limit=50`) |
| POST | `/api/chat` | Send a message; receive the complete response and executed actions |

### Business Rules

**Ticker format:** uppercase after normalization; matches `^[A-Z]{1,5}(\.[A-Z])?$` (covers `BRK.B`). Anything else → `400 invalid_request`.

**Ticker existence:** the simulator accepts any well-formed symbol. In Massive mode, `add_ticker` must succeed on the prev-close lookup, otherwise → `404 unknown_ticker`.

**Fill rule:** a market order fills at the latest price in the cache at the moment the request is handled. If the cache has no price for the ticker (for example, a Massive ticker before its first poll) → `409 price_unavailable`. Nothing is filled.

**Concurrency:** all trade executions (manual and LLM) pass through one `asyncio.Lock`. The cash check, the write, and the snapshot happen inside it, so two requests cannot overspend.

**Quantity:** `> 0`, at most 4 decimal places, at most 1,000,000 shares per trade. Converted to `quantity_e4 = round(quantity × 10000)`. Violations → `400 invalid_request`.

**Buy:** `amount_cents = round_half_up(quantity_e4 × fill_price / 100)`. Insufficient cash (`amount_cents > cash_cents`) → `409 insufficient_cash`. Average cost is recomputed in floating point for display only: `new_avg = (old_qty·old_avg + qty·price) / (old_qty + qty)`. Cash arithmetic never reads `avg_cost`.

**Sell:** `amount_cents = round_half_up(quantity_e4 × fill_price / 100)`. Insufficient shares (`quantity_e4 > held_e4`) → `409 insufficient_shares`. Exact integer comparison, so there is no tolerance to tune. A sell that empties the position deletes the row. Average cost does not change on a sell.

**Rounding:** `round_half_up` is used for every money conversion. Cash never leaves integer cents. Rounding is applied once per trade, to `amount_cents`, and never to running totals.

**Reset:** deletes trades, positions, watchlist, chat messages, snapshots, and `price_state`, then re-runs the seed. It takes the same lock as trades. Live prices are not reset; the simulator keeps running.

**Snapshots:** total value = cash + Σ(quantity × price), with `price` as in the Restart Rules. Recorded after each trade and every 30 s. Stored as `total_value_cents`.

**Chat rate limit:** `POST /api/chat` accepts 10 requests per minute. Excess requests return `429 rate_limited` with `Retry-After`.

**Host allowlist:** requests whose `Host` header is not localhost or 127.0.0.1 are rejected (API.md conventions).

---

## 9. LLM Integration

### Model and Transport

The backend calls `openrouter/openai/gpt-oss-120b` through LiteLLM and OpenRouter, with the provider pinned to Cerebras (`provider: {order: ["cerebras"], allow_fallbacks: false}`). The API key is read from `OPENROUTER_API_KEY`. It is never sent to the frontend.

Structured output is requested with `response_format` of type `json_schema` (strict). *Spike S2 (Section 13.5) confirms that this combination is honoured for this model and provider.* If the spike fails, fall back to `json_object` with the same Pydantic schema. Server-side validation is mandatory in both cases. A 200 response with empty or non-conforming content is treated as malformed.

### How It Works

When the user sends a chat message, the backend:

1. Loads portfolio context: cash, positions with P&L, watchlist with live prices, total portfolio value.
2. Loads the last **20** messages from `chat_messages`.
3. Builds the prompt: system message, portfolio context, history, new user message.
4. Calls the LLM, requesting the structured schema below.
5. Parses and validates the response with Pydantic (Section "Validation" below).
6. Stores the user message, then executes actions in this order:
   1. Watchlist changes, in array order (so "add PYPL and buy 5" works in one turn)
   2. Trades, in array order
7. Stores the assistant message with `actions` set to the executed results.
8. Returns the complete JSON response. There is no token streaming; a loading indicator is enough.

### Structured Output Schema

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

- `message` (required, string, 1–2000 characters)
- `trades` (optional, array, max 10): each item `{ticker, side ∈ {buy, sell}, quantity > 0}`
- `watchlist_changes` (optional, array, max 10): each item `{ticker, action ∈ {add, remove}}`

### Execution Semantics

- **Non-atomic and independent.** Each item executes on its own, in order, through the same code path as a manual trade or watchlist change, with the same validation, lock, and fill rule. If the LLM returns three trades and the second fails (say, insufficient cash), the first and third still execute. Each item's result is reported separately.
- **Per-item result shape** (in both the stored `actions` and the API response):
  - Trade: `{ticker, side, quantity, status: "filled"|"rejected", price?, error?}`
  - Watchlist change: `{ticker, action, status: "applied"|"rejected", error?}`
- **Rejections are data, not exceptions.** The error code and message go back to the user in the chat response, and into the next prompt, so the LLM can explain what happened.

### Validation and Failure Handling

- Pydantic model enforces the schema. Ticker format and quantity bounds are checked by the same rules as the REST API (Section 8).
- **Malformed or non-conforming response:** one automatic retry, with the validation error appended to the prompt.
- **Second failure:** return a fixed assistant message, "I couldn't complete that request. No trades were placed." with `actions: null`. Nothing executes. The raw model output is logged server-side, not stored.
- **Missing key:** `POST /api/chat` returns `503 llm_unavailable`. The rest of the app works.

### Auto-Execution

Trades execute automatically with no confirmation dialog. This is deliberate:

- The environment is simulated, so stakes are zero.
- It makes the demo fluid.
- It demonstrates agentic behavior, the course's core theme.

Safety does not depend on the LLM behaving. Every action passes the same server-side validation as a manual order, and nothing else in the system can cause a trade.

### System Prompt Guidance

The LLM is prompted as "FinAlly, an AI trading assistant" and instructed to:

- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven
- Respond only with JSON matching the schema

The prompt does not contain secrets. User-supplied text is treated as data and is never rendered as HTML.

### LLM Mock Mode

When `LLM_MOCK=true`, the backend bypasses OpenRouter and returns deterministic responses:

- `"buy <TICKER> <N>"` in the message → a single matching trade, with a short confirmation message
- `"add <TICKER>"` / `"remove <TICKER>"` → the matching watchlist change
- Anything else → a fixed analysis message with no actions

This supports fast, free, reproducible E2E tests, offline development, and CI.

---

## 10. Frontend Design

### Layout

A single-page application with a dense, terminal-style grid. The component architecture is up to the frontend agent. The UI must include:

- **Watchlist panel**: ticker, current price (flashing on change), daily change % (vs `prev_close`), and an SVG sparkline of the last 600 points. Clicking a row selects it.
- **Main chart area**: price line for the selected ticker, with the history from the snapshot plus live points. Title shows ticker, last price, and change %.
- **Portfolio heatmap**: treemap of positions (Section 10, "Charts").
- **P&L chart**: total portfolio value over time, from `/api/portfolio/history` plus live updates.
- **Positions table**: ticker, quantity, avg cost, current price, unrealized P&L, % change.
- **Trade bar**: ticker field, quantity field, Buy button, Sell button. Market orders, instant fill, no confirmation.
- **AI chat panel**: docked, collapsible sidebar. History from `GET /api/chat/messages`, input, loading indicator, and inline confirmations for executed trades and watchlist changes, including rejections with their error text.
- **Header**: live total portfolio value, cash balance, connection indicator, and a Reset button (confirm dialog).

### Charts

- **Line charts** (main price chart, P&L chart): Lightweight Charts, which is canvas-based and performs well at 2 updates/s across many points.
- **Time axis rule (main chart):** Lightweight Charts requires strictly ascending integer-second timestamps with no duplicates. The stream arrives every 500 ms, so the frontend buckets points to one per second before calling the library: the last price within each second wins, and `time = floor(ts / 1000)`. The snapshot history is bucketed the same way on load. The P&L chart receives one point per snapshot (every 30 s), so it needs no bucketing; if live points are appended, they go through the same one-per-second rule.
- **Sparklines**: inline SVG `<polyline>`. They are cheap, and Lightweight Charts is too heavy for ten small instances.
- **Treemap**: a squarified layout from `d3-hierarchy` (layout only), rendered as absolutely positioned HTML/SVG elements. Recharts' Treemap is an acceptable substitute if the team prefers a single library, but it is SVG-based and slower to update.

### Colors and Interactions

- **Buy button**: `#209dd7` (blue). **Sell button**: `#ecad0a` (yellow). **Chat send / watchlist add**: `#753991` (purple).
- **Heatmap:** size = market-value weight. Color = unrealized % return vs average cost, saturating at ±10%, with a linear scale from neutral gray `#30363d` at 0% to full gain or loss color at the saturation point. Gain/loss colors are the Section 2 palette values.
- **Price flash:** on change, apply a class that sets background to gain or loss color, then removes it after 500 ms. Direction comes from the `direction` field, not by comparing values in the component.
- **Live total:** computed in the frontend as `cash + Σ(quantity × live price)` from SSE prices and the positions list. Positions are re-fetched from `/api/portfolio` after each trade. The backend is authoritative for trades and snapshots; the frontend valuation is a display convenience and must match the backend to the cent at the same prices. A pure function with a unit test covers this.

### Connection Status

The header dot reflects `EventSource` state, heartbeat freshness, and the upstream source status. Heartbeats arrive every 5 s whether or not prices move, so a quiet market does not turn the dot yellow:

| State | Condition | Color |
|---|---|---|
| Connected | `readyState === OPEN`, a `heartbeat` (or `prices` or `snapshot`) seen in the last 20 s, and `source_status === "ok"` | Green |
| Degraded | `readyState === OPEN`, heartbeat fresh, but `source_status === "degraded"` (market source failing) | Yellow |
| Reconnecting | `readyState === CONNECTING`, or no event of any kind for 20 s while `OPEN` | Yellow |
| Disconnected | not `OPEN` continuously for 30 s | Red |

### Technical Notes

- Use `EventSource` for `/api/stream/prices`. On each `snapshot`, replace state entirely.
- All API calls go to the same origin (`/api/*`). No CORS.
- Tailwind CSS with a custom dark theme built from the color tokens above.
- Escape all LLM output and user text. Never use `dangerouslySetInnerHTML`.

---

## 11. Docker & Deployment

### Multi-Stage Dockerfile

```
Stage 1: node:22-slim (Node 22 active LTS; pin the minor version in the tag, e.g. node:22.x-slim)
  - Copy frontend/
  - npm ci && npm run build  → static export in frontend/out

Stage 2: python:3.12-slim
  - Install uv
  - Copy backend/ and run `uv sync --frozen`
  - Copy frontend/out → /app/static
  - Expose 8000
  - HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
      CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
    (Python is used because python:3.12-slim does not ship curl.)
  - CMD: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

FastAPI serves the static files and all API routes on port 8000. `--workers 1` is required (Section 3).

### Docker Volume (Bind Mount)

The database lives in a bind mount, so students can inspect it directly:

```bash
docker run --name finally -p 127.0.0.1:8000:8000 \
  -v "$(pwd)/db:/app/db" \
  --env-file .env \
  finally
```

- **Loopback only.** The port is published on `127.0.0.1`, not on all interfaces, so nothing on the LAN can reach the app. The server also rejects requests whose `Host` header is not localhost (DNS-rebinding defense). The app is not designed for exposure beyond localhost, and there is no authentication.

- The host `db/` directory maps to `/app/db`. The backend writes `finally.db` there.
- Windows: Docker Desktop with the WSL 2 backend. The start script resolves the absolute path. The default rollback journal avoids WAL file issues on shared mounts.
- `.dockerignore` excludes `.env`, `db/`, `frontend/node_modules`, and `.git`, so secrets and state never enter the image.

### Start/Stop Scripts

Container name is fixed: **`finally`**.

**`scripts/start_mac.sh`** and **`scripts/start_windows.ps1`**:

1. If container `finally` is already running → print the URL, exit 0.
2. If it exists but is stopped → `docker start finally`, print the URL, exit 0.
3. Otherwise: build the image if missing or if `--build` is passed, then `docker run` as above.
4. Check that host port 8000 is free before running. If not, exit 1 with the name of the blocking process where possible.
5. Print `http://localhost:8000`. Open the browser unless `--no-browser` is passed.

**`scripts/stop_mac.sh`** and **`scripts/stop_windows.ps1`**:

- Stop and remove container `finally` if it exists. Exit 0 if it does not.
- Never touch `db/`. Data persists across stop and start.

**Idempotence** means: running a start or stop script any number of times yields the same end state, and the exit code is 0 whenever the desired state already holds. Scripts never delete data. The only way to reset data is the in-app Reset (Section 8).

### Deployment Targets

The image is portable to any container platform that provides a **persistent volume** mounted at `/app/db`. Examples: a VM or local Docker host, Render with a persistent disk, Fly.io with a volume, or ECS on Fargate with an EFS mount.

AWS App Runner is **not** a target. It does not currently provide a persistent volume mount, so `finally.db` would be lost on every redeploy or restart.

Infrastructure-as-code for any cloud is out of scope for this spec.

---

## 12. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**

- **Market data — simulator**
  - Prices are positive and finite; tick cadence matches config.
  - All statistical tests run with `SIM_EVENTS_ENABLED=false`, so jumps do not contaminate the oracles.
  - *Drift (exact):* with σ = 0, one step's log-return equals `μ·Δt` to within 1e-12.
  - *Diffusion (variance):* with a fixed seed over 100,000 steps, the sample variance of log-returns is within 2% of `σ²·Δt`.
  - *Itô term (power check):* the `−σ²/2` term is tested with a large Δt (1/252) and large σ (e.g. 0.8), where the term is larger than the standard error. The test must fail if the term is dropped.
  - *Drift (power check):* for a large μ (e.g. 1.0) over a long run, the sample mean of log-returns is within 4 standard errors of `(μ − σ²/2)·Δt`, and the test must reject μ = 0 and μ = −1.0 at the same tolerance. A test that cannot distinguish these is a failing test.
  - *Correlation:* the sample correlation between two tickers with betas β_i and β_j is within 0.02 of `β_i·β_j`.
  - *Events:* with σ = 0 (diffusion off), the number of jumps per ticker over N ticks is within binomial bounds for p = 1/1200. Each jump magnitude is in [2%, 5%], and the sign is random.
  - Unknown symbols use the default parameters.
- **Market data — Massive**
  - Parsing the batched snapshot response into `PriceUpdate`s.
  - Chunking at 250 symbols.
  - One retry rule: 429 and 5xx keep last prices and do not raise.
  - `UnknownTicker` raised on not-found.
- **Both sources** conform to the `PriceSource` interface (shared contract test).
- **Portfolio**
  - Buy: cash decreases by cost (to the cent); weighted average cost is correct.
  - Sell: average cost unchanged; full sell deletes the row; `0.1 + 0.2 − 0.3` residue does not leave a row.
  - Selling more than owned → `insufficient_shares`. Buying with insufficient cash → `insufficient_cash`.
  - Selling at a loss updates cash and P&L correctly.
  - Quantity bounds: zero, negative, more than 4 dp, over 1,000,000 → `invalid_request`.
  - Concurrency: N concurrent buys against limited cash never overspend.
  - **Invariants (Section 7)** hold after a randomized sequence of trades, with exact integer equality: `cash_cents` equals the replayed sum of `amount_cents`, and positions rebuilt from `trades` equal the `positions` table.
  - *Sell tolerance:* buy 0.1 and 0.2, then sell 0.3, succeeds. Integer `quantity_e4` makes this exact.
  - *Restart:* after a simulated restart, a held ticker resumes from its `price_state` price, and the snapshot is not recorded as zero for a missing price.
  - *Host allowlist:* a request with `Host: evil.example` is rejected with `400 invalid_request`.
  - *Rate limit:* the 11th `POST /api/chat` in a minute returns `429 rate_limited`.
- **LLM**
  - Valid schema parses; malformed JSON, missing `message`, unknown `side`, and oversize arrays are rejected.
  - Retry-once behavior, then the fixed fallback message.
  - Watchlist changes execute before trades; a failing trade does not block later items.
  - Mock mode returns the documented deterministic responses.
- **API routes**: status codes and response shapes match `API.md` for every endpoint, including each error code.
- **Store**: lazy initialization creates the schema and seed exactly once; a second start does not reseed; the seed snapshot exists.

**Frontend (React Testing Library or similar)**

- Component rendering with mock data
- Price flash triggers on `direction` up and down, and clears after 500 ms
- Watchlist add/remove
- Live total valuation matches a backend-computed fixture at the same prices
- Heatmap color mapping: 0% → neutral; +10% and beyond → full gain; −10% and beyond → full loss
- Chat rendering, loading state, and inline rejection messages
- Connection indicator state transitions (Section 10): green on heartbeat; yellow after 20 s of silence with the stream open; yellow when `source_status` is `degraded`; red after 30 s not open
- Main chart bucketing: 600 points at 500 ms spacing produce 300 strictly increasing integer-second points, with the last price per second kept
- Heartbeat handling: heartbeats update liveness without changing prices

### E2E Tests (in `test/`)

**Infrastructure:** `docker-compose.test.yml` in `test/` starts the app container and a Playwright container. Browser dependencies stay out of the production image.

**Environment:** `LLM_MOCK=true` and the simulator, by default. Deterministic and fast.

**Key scenarios:**

- Fresh start: default watchlist appears, $10,000 cash shown, prices streaming
- Add and remove a ticker from the watchlist
- Buy: cash decreases, position appears, total value updates
- Sell: cash increases, position updates or disappears
- Heatmap renders with correct colors; P&L chart has data points
- AI chat (mocked): send a message, receive a response, trade execution appears inline
- Chat rejection: a buy with insufficient cash shows the error inline
- SSE resilience: drop the connection, verify the indicator goes yellow, then green, and that state is correct after the new `snapshot`
- Quiet stream stays green: in Massive mode between polls with no price changes, the indicator stays green for at least 60 s because of heartbeats
- Degraded source: a mocked Massive failure for 3 polls shows yellow with `source_status: degraded`, and it returns to green after one successful poll
- Reset: after trades and chat, Reset returns to the seed state

### Live LLM Smoke Test

One test in `test/live_llm_smoke/` sends a fixed prompt through the real OpenRouter path and asserts that the response parses against the schema. It runs only when `OPENROUTER_API_KEY` is present, nightly and before each release. It is **not** in the PR gate, so flaky network or provider issues never block merges.

---

## 13. Review Resolutions (doc-review, 2026-11-18)

Each item from the review is resolved below. Where a decision needs external verification, it is listed in Section 13.5 as a spike.

### 13.1 Questions

| # | Resolution | Section |
|---|---|---|
| 1 | Yes. Every endpoint has request/response examples, status codes, and one error shape. | [API.md](API.md) |
| 2 | One `prices` event per tick with changed tickers; a `snapshot` event on connect; liveness via heartbeat. *Superseded in part by 13.6 C1: heartbeat is a named event, not a comment.* | §6 |
| 3 | Batched multi-ticker snapshot: one call per poll for all tickers, chunked at 250. Confirmed by spike S1. | §6 |
| 4 | Massive is the rebranded Polygon.io API. Base URL is pinned in the client, not an env var. | §5, §6 |
| 5 | Simulator accepts any well-formed symbol with default parameters (μ 5%, σ 25%, β 0.5, start $100.00). | §6 |
| 6 | Format check → 400. Simulator accepts. Massive mode checks prev-close lookup → 404 `unknown_ticker`. | §8 |
| 7 | Baseline is previous session close. Massive fetches it once per ticker per day. Simulator starts at the seed price, so change begins at 0%. | §6 |
| 8 | Backend keeps a 600-point rolling history per ticker. It is delivered in the `snapshot` on connect. | §6 |
| 9 | Frontend computes live total from SSE and positions. Backend is authoritative for trades and snapshots. | §10 |
| 10 | Non-atomic: each item executes independently, in order, with per-item results. | §9 |
| 11 | Yes. `action` is an explicit enum: `add` \| `remove`. | §9 |
| 12 | Primary: `json_schema` strict, provider pinned to Cerebras. Fallback: `json_object` with the same Pydantic validation. Decided by spike S2. | §9, §13.5 |
| 13 | One automatic retry with the error appended. Then a fixed fallback message, no trades executed, raw output logged, not stored. | §9 |
| 14 | Last 20 messages. | §9 |
| 15 | Yes. First initialization writes one $10,000 snapshot. | §7 |
| 16 | Retain 7 days; the snapshot task prunes older rows. | §7 |
| 17 | Yes. `POST /api/portfolio/reset` with `{"confirm":"RESET"}`, plus a header button behind a confirmation. | §8, §10 |
| 18 | Quantity > 0, ≤ 4 dp, ≤ 1,000,000 per trade. Stored as integer ten-thousandths; full sells delete the row. Cash stored as integer cents. *Superseded in detail by 13.6 H6/H7.* | §7, §8 |
| 19 | Confirmed: weighted average on buy, unchanged on sell, zero-quantity rows deleted. Realized P&L is not tracked (non-goal). | §2, §8 |
| 20 | Buy `#209dd7` blue, Sell `#ecad0a` yellow, submit purple. Green and red are reserved for P&L and flashes. | §2, §10 |
| 21 | Color = unrealized % vs average cost, saturating at ±10%. Zero = neutral gray `#30363d`. Size = market-value weight. | §10 |
| 22 | Yellow while `EventSource` is connecting or silent for 20 s. Red if not open for 30 s continuously. | §10 |
| 23 | One live smoke test, nightly and pre-release when the secret is present. Not in the PR gate. | §12 |
| 24 | Container is named `finally`. | §11 |

### 13.2 Clarifications

| # | Resolution | Section |
|---|---|---|
| 1 | Bind mount `./db:/app/db` for inspectability. Default rollback journal to avoid WAL on shared mounts. | §11 |
| 2 | Container reads env only, via `--env-file`. The image excludes `.env`. Local non-container dev uses `load_dotenv()`. | §5 |
| 3 | Renamed `backend/db/` → `backend/app/store/`. | §4 |
| 4 | Start scripts open the browser by default. `--no-browser` suppresses. The server never opens a browser. | §2, §11 |
| 5 | Initialization runs at startup, before serving. `/api/health` reports DB readiness. | §7 |
| 6 | Interval is an env var, `MASSIVE_POLL_INTERVAL_SECONDS`, default 15. | §5 |
| 7 | `previous_price` = value before the latest cache update, at any cadence. Flashes are infrequent in Massive mode, by design. | §6 |
| 8 | Lines: Lightweight Charts (canvas). Sparklines: SVG. Treemap: `d3-hierarchy` layout, rendered as HTML/SVG. | §10 |
| 9 | Rationale rewritten: zero configuration, single transactional file. `user_id` kept for future multi-user. | §3 |
| 10 | Process instructions moved to `AGENTS.md`. | §1, §9 → AGENTS.md |
| 11 | Working-tree deletions flagged as open item O1. | §13.5 |
| 12 | App Runner removed from deployment targets: no persistent volume support. | §11 |
| 13 | Removed the empty "Project Specification" heading. The subtitle now carries that role. | Top |

### 13.3 Feedback

| # | Resolution | Section |
|---|---|---|
| 1 | Process content split into `planning/AGENTS.md`. PLAN.md describes product and contracts. | AGENTS.md |
| 2 | Contract is `planning/API.md`, linked from §8. | [API.md](API.md) |
| 3 | Fill rule defined: latest cached price at request time; `409 price_unavailable` if none. Serialized by one lock. | §8 |
| 4 | `.env.example` contents specified. | §5 |
| 5 | Idempotence defined for start and stop scripts. | §11 |
| 6 | Correlation mechanism: one market factor plus per-ticker β. Testable via the expected correlation. | §6 |
| 7 | Events: jump 2–5%, random sign. Probability set to 1/1200 per ticker per tick (≈ once per 10 minutes). *Superseded by 13.6 H1.* | §6 |
| 8 | GBM oracle: mean and variance of log-returns over 100k seeded steps, with stated tolerances. | §12 |
| 9 | Terraform/`deploy/` removed from the core spec. | §11 |
| 10 | Non-goals list added. | §2 |

### 13.4 Simplifications

| # | Resolution | Section |
|---|---|---|
| 1 | Watchlist and positions use `(user_id, ticker)` as primary key. Append-only tables use INTEGER autoincrement. | §7 |
| 2 | Kept `users_profile` with a stated invariant: exactly one row. | §7 |
| 3 | `GET /api/watchlist` returns tickers only. Prices come from the SSE snapshot. | §8 |
| 4 | Single market factor plus per-ticker β. | §6 |
| 5 | Massive adapter has one call shape, one parser, one retry rule. Interval is configurable, default 15 s. | §6 |
| 6 | "Why These Choices" reduced to three rows and a paragraph. | §3 |
| 7 | Chat response object is stored verbatim as `actions`. | §7, §9 |
| 8 | `trades` is authoritative; `positions` is a rebuildable cache, enforced by an invariant test. | §7, §12 |
| 9 | Key Boundaries merged into the directory tree. | §4 |

### 13.5 Open Items

These need verification or a decision before the affected code is locked.

- **S1 — Massive API facts (expanded per review C3).** Verify against the live Massive docs, and record the answers in this section before the market client is written:
  - (a) Which plan tiers serve the multi-ticker snapshot endpoint, and whether the free tier serves it at all. If not, Massive mode requires a paid tier and `.env.example` says so.
  - (b) The per-request symbol limit for the snapshot endpoint. Sources conflict (one says none, another says 250). Use the confirmed value, and chunk at the lower of 250 and that value.
  - (c) Whether the snapshot payload includes `prevDay.c`. If yes, `prev_close` comes from the snapshot and the per-ticker baseline call is dropped.
  - (d) The `/prev` response for an unknown symbol: `404`, or `200` with zero results. Both are already handled as not-found (PLAN §6).
  - (e) The current base URL (`api.massive.com` or the Polygon.io host). Pin the confirmed value in the client.
  - Fallback if (b) is lower than expected: chunk at that size and re-measure the call budget against `MASSIVE_CALLS_PER_MINUTE`.
- **S2 — Structured outputs on OpenRouter/Cerebras.** Confirm that `json_schema` (strict) is honoured for `openai/gpt-oss-120b` with the Cerebras provider pinned. One third-party report describes an empty-content 200 for strict schemas. If confirmed, use `json_object` plus validation. The retry-once rule in Section 9 covers either outcome.
- **O1 — Working-tree deletions: resolved.** The deletion of `backend/` (including `pyproject.toml`, `app/`, `tests/`), `planning/archive/`, and `.github/workflows/` is intentional and permanent. The previous backend code is not to be restored, referenced, or reused. `backend/` is built from scratch from this spec, and agents must not recover files from git history (HEAD `14550e1`) to fill gaps. The old `MARKET_*` planning docs are likewise gone; `planning/API.md` and `planning/PLAN.md` are the only sources.

### 13.6 Reviewer Findings: Critical and High (applied 2026-11-18)

Source: `planning/REVIEW.md`, written by the `reviewer` agent. Medium and low findings are **not yet applied** and remain open in `REVIEW.md`.

| ID | Resolution | Section |
|---|---|---|
| C1 | Liveness moved from SSE comments to a named `heartbeat` event every 5 s, always sent. Indicator uses heartbeat freshness. | §6, §10, API.md |
| C2 | `heartbeat` carries `source_status` (`ok` / `degraded`) and `consecutive_failures`. Degraded after 3 consecutive failed polls, yellow dot, recovers after one success. | §6, §10, API.md |
| C3 | Client-side token-bucket limiter for all Massive calls (`MASSIVE_CALLS_PER_MINUTE`, default 5). `prev_close` taken from the snapshot if S1 confirms `prevDay`, otherwise a lowest-priority per-ticker call. Empty results count as not-found. Spike S1 expanded to plan tier, unknown-symbol response, and base URL. | §5, §6, §13.5 |
| H1 | Jump probability lowered to 1/1200 per tick; jumps are multiplicative and injectable (`jump_prob`, `SIM_EVENTS_ENABLED`). Effective volatility with jumps on (≈50%) is documented as a product decision. Statistical tests run with jumps off. | §6, §12 |
| H2 | Drift tested exactly with σ = 0. Itô term and drift tested with power checks that must reject wrong values. | §12 |
| H3 | Main chart buckets to one point per second with integer-second times. Unit test added. | §10, §12 |
| H4 | Port published on `127.0.0.1` only. `Host` allowlist (localhost / 127.0.0.1). `415` for non-JSON bodies. `429 rate_limited` on `/api/chat` (10/min). Stated as not designed for exposure beyond localhost. | §2, §8, §11, API.md |
| H5 | Healthcheck uses Python `urllib`, not `curl`, with a start period. | §11 |
| H6 | Money stored as integer cents (`cash_cents`, `amount_cents`, `total_value_cents`); quantities as integer ten-thousandths (`quantity_e4`). Invariants are exact equality. | §7, §8, §12 |
| H7 | Moot under integer quantities. Exact comparison, plus a 0.1 + 0.2 sell test. | §8, §12 |
| H8 | `price_state` table persists last price and `prev_close`. Restart rules define simulator resume, Massive `stale` values, and snapshot skipping. `current_price` and derived fields are nullable with `stale` flag. | §6, §7, API.md |
| H9 | Base image moved to `node:22-slim` (pinned minor). | §11 |
