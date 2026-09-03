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
| GET | `/api/trades` | Trade history (blotter panel + LLM context) |

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
| GET | `/api/chat/history` | Recent conversation history, so the chat panel survives a page refresh |

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

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value), **recent trade history, and a downsampled portfolio value trajectory** so the assistant can discuss what was done and how the portfolio has moved, not only its current state — see `planning/API_CONTRACT.md` §6 for the exact context block
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

## 13. Review Notes — Questions, Clarifications & Simplifications

*Added 2026-09-02. Reviewed against the current repo state: `backend/app/market/` is complete (see `planning/MARKET_DATA_SUMMARY.md`); `frontend/`, `test/`, `scripts/`, and the Dockerfile do not exist yet. Items are grouped by how much they cost to fix later.*

### 13.1 Contradictions & gaps that will bite during the build

**1. "Daily change %" has no baseline anywhere in the design.**
Section 10 asks the watchlist for a *daily* change %, but the shipped `PriceUpdate` (`backend/app/market/models.py`) only carries `previous_price` from the **previous tick** — its `change_percent` is a ~500ms delta, typically ±0.05%, which will render as a permanently-flat column. Nothing in the schema, cache, or seed data stores a session open or previous close.
**Recommendation:** add `open_price` to `PriceUpdate` and the cache — for the simulator use the seed price as the session open; for Massive use the previous close from the API. Then expose **two** distinct fields and name them unambiguously: `tick_direction` (drives the flash animation) and `change_percent_today` (drives the column). Decide this before the frontend starts, because both consume the same SSE payload.

**2. The trade-failure feedback loop is logically impossible as written.**
Section 9 says trades execute *after* the LLM response is parsed, and that "the error is included in the chat response so the LLM can inform the user." The LLM has already finished writing its message by then — it cannot inform anyone about a failure that happens later.
**Recommendation:** pick one — (a) return per-action results in the `actions` JSON and have the **frontend** render failures as inline red chips under the message (simplest, no extra latency, recommended); or (b) do a cheap second LLM call only when an action failed. Do not leave this ambiguous or the agent will silently swallow failed trades.

**3. Docker volume: the two paragraphs describe different things.**
Section 4 says the repo's `db/` directory "maps to `/app/db` in the container", but the command in Section 11 mounts a **named volume** (`-v finally-data:/app/db`), which does not touch the host `db/` directory at all.
**Recommendation:** commit to the named volume (it is the right call — no host permission issues on macOS/Windows) and restate `db/.gitkeep` as being for *local, non-Docker* development only.

**4. `backend/db/` would be excluded from the built wheel.**
Section 4 puts schema SQL and seed logic in `backend/db/`, but the already-committed `backend/pyproject.toml` has `[tool.hatch.build.targets.wheel] packages = ["app"]`. Anything outside `backend/app/` is not packaged, and the `backend/db/` vs root `db/` naming is a trap agents will fall into.
**Recommendation:** move it to `backend/app/db/` (importable, packaged, unambiguous) and reserve the name `db/` exclusively for the runtime volume.

**5. Lazy DB init "on first request" starves the market data source.**
Section 7 allows init on startup *or* first request, but the market data source must be started with the watchlist read from the database, and that happens in the app lifespan — before any request arrives.
**Recommendation:** delete the "or first request" option. Initialize and seed the DB in the FastAPI lifespan handler, then start the market source from the seeded watchlist. One code path, no request-time locking.

**6. Timestamp formats are inconsistent across the API surface.**
The DB columns are ISO strings; `PriceUpdate.timestamp` is a Unix float. The frontend will receive both from different endpoints and have to branch.
**Recommendation:** state one wire convention in this document — suggest **ISO 8601 UTC strings everywhere in JSON**, with the SSE payload converted at serialization time.

**7. The SSE stream has no heartbeat, which matters most in Massive mode.**
`stream.py` only emits when the cache version changes. With the simulator that is every 500ms, but with Massive polling at 15s (free tier) the connection sits silent for 15 seconds at a time, and if the market is closed it is silent *forever* — long enough for intermediate proxies or a laptop sleep to kill it without either side noticing.
**Recommendation:** emit an SSE comment (`: ping\n\n`) every ~10s when there is nothing to send.

### 13.2 Questions that need a product decision

**8. What happens when a user adds a ticker the simulator has never heard of?**
`seed_prices.py` has hand-tuned seed prices and GBM parameters for a fixed set. `POST /api/watchlist {"ticker": "ZZZZ"}` — and the LLM's `watchlist_changes` — can request anything. Reject unknown symbols against an allowlist, or synthesize a plausible seed price and default volatility? (Recommend: synthesize, with a default vol, so the demo never dead-ends — but say so explicitly.) In Massive mode, what is the behavior for a symbol the API rejects?

**9. Can you trade a ticker that is not on the watchlist?**
The trade bar has a free-text ticker field, and pricing a trade requires a cache entry. Recommend: auto-add to the watchlist on trade. Either way, state it.

**10. Can you remove a ticker you hold a position in?**
Today that would call `source.remove_ticker()`, prices stop flowing, and the position's current price, unrealized P&L, heatmap tile, and total portfolio value all go stale or null. Recommend: the set of tracked tickers is **watchlist ∪ held positions**; removing from the watchlist only hides the row.

**11. Is realized P&L needed?** The schema tracks only positions and trades; after a sell, the gain disappears into `cash_balance` with nothing to show for it. The chat assistant is asked to "analyze P&L". Recommend: skip a realized-P&L column, and let the chat context derive it from the `trades` table if asked.

**12. Is trade history surfaced anywhere?** The `trades` table has no API endpoint and no UI element in Section 10. Is it audit-only, or should `GET /api/trades` exist for the chat context and a history panel?
**Decided:** expose it. `GET /api/trades` is specified in `planning/API_CONTRACT.md` §3 and feeds both a blotter panel and the LLM context.

**13. What does the app do when `OPENROUTER_API_KEY` is missing?** It is listed as "required", but everything except chat works fine without it. Recommend: boot normally, and have `/api/chat` return a friendly "chat unavailable — no API key" message rather than a 500.

**14. Massive mode outside market hours** — on a weekend the entire app is frozen, which is a bad first impression for a student running the demo. Document it, or auto-fall-back to the simulator when the last quote is more than N minutes stale.

**15. Background price history for charts.** The main chart and the sparklines accumulate from SSE "since page load", so on first paint the chart is empty and a refresh throws it all away. Is that acceptable for the demo, or should the backend keep a bounded in-memory ring buffer (e.g. last 600 ticks per ticker) behind `GET /api/prices/{ticker}/history`? Recommend the ring buffer — it is ~30 lines, needs no schema change, and is the difference between a chart that looks alive on load and one that looks broken.

### 13.3 Under-specified behavior agents will otherwise invent differently

- **16. Money precision.** `REAL` columns and float math will produce `9999.999999999998` cash balances. State the rule: full float precision in storage, rounded to 2dp on display and to 4dp on quantities.
- **17. Zeroed positions.** Section 12 says a position "updates or disappears". Pick: delete the row when quantity falls below an epsilon (`< 1e-9`), never keep a zero-quantity row.
- **18. Trade validation rules**, stated once and shared by the manual and LLM paths: quantity > 0, no shorting, no margin (cash may not go negative), sell quantity ≤ held quantity, ticker must be priceable.
- **19. Concurrency.** Manual trades and LLM trades can execute simultaneously against the same SQLite file. Wrap trade execution in a single `asyncio.Lock` plus a DB transaction — a couple of lines that eliminate a whole class of race conditions.
- **20. LLM action caps.** Auto-execution with no confirmation is a good product decision, but there is no ceiling on it. Cap it: max ~5 trades and ~5 watchlist changes per message, each individually validated.
- **21. Chat history window.** "Recent conversation history" needs a number — suggest the last 20 messages, truncated by count not tokens.
- **22. `actions` JSON shape.** The frontend renders this column; define it here, e.g. `[{"type":"trade","ticker":"AAPL","side":"buy","quantity":10,"status":"ok"|"failed","price":190.5,"error":null}]`.
- **23. `POST /api/portfolio/trade` response shape.** Have it return the full updated portfolio so the frontend needs no follow-up GET.
- **24. `GET /api/portfolio/history` query params.** Define `?limit=` / `?since=` now — snapshots accrue at 2,880/day and the P&L chart should not fetch a week of them.
- **25. Structured-output fallback.** Confirm that `openrouter/openai/gpt-oss-120b` on the Cerebras provider honors strict JSON-schema `response_format`; providers vary. Specify the fallback: validate with Pydantic, and on a parse failure do exactly one repair retry, then return a plain-text message with no actions.
- **26. Color tokens.** Section 2 offers `#0d1117` *or* `#1a1a2e` and names three accent colors, but the P&L green/red are never given hex values. Pick one background and pin the semantic colors (up/green, down/red, plus a muted border) so the watchlist, heatmap, and P&L chart agree.
- **27. Minimum supported width.** "Desktop-first, functional on tablet" — give a number (e.g. degrade gracefully to 1024px, no mobile layout) or an agent will spend a day on responsive breakpoints nobody asked for.
- **28. Static-export constraints.** `output: 'export'` rules out server components, route handlers, middleware, and the Next.js image optimizer. Also worth stating: FastAPI must mount `/api/*` **before** the static catch-all, and unknown paths should fall back to `index.html`.
- **29. Background tasks.** The Section 3 diagram lists only the market data task; there are two (market data + the 30s portfolio snapshot). Also: take a snapshot at startup so the P&L chart is not empty for its first 30 seconds.

### 13.4 Opportunities to simplify


- **31. Cut the "SSE resilience: disconnect and verify reconnection" E2E test.** Forcing a mid-stream disconnect from Playwright requires CDP network fiddling or route interception, and what it ultimately verifies is that the browser's built-in `EventSource` retry works. Cover the server side with a unit test on `_generate_events` and drop the E2E case.
- **32. Simplify `GET /api/watchlist`.** Returning "tickers with latest prices" duplicates data the SSE stream sends 500ms later. It is worth keeping *only* to avoid a blank first paint — if so, say that is its purpose; otherwise return tickers alone and let SSE own all pricing.
- **33. One env-loading mechanism, not two.** Section 5 says the backend reads `.env` from the project root; Section 11 passes `--env-file .env` to Docker. Use `--env-file` for the container and `python-dotenv` only as a local-dev convenience, and say so.
- **34. Drop `docker-compose.yml`.** Section 3 explicitly argues against compose ("no docker-compose for production"), and Section 4 then lists an optional one. With the start/stop scripts already wrapping `docker run`, the compose file is a third way to launch the same container. Delete it from the structure.
- **35. Reduce the four start/stop scripts to two.** `start_mac.sh` / `start_windows.ps1` differ only in shell syntax around one `docker run` line. Consider a single `scripts/start.sh` plus a thin `start.ps1` that documents the same command — or accept the duplication but explicitly mark the PowerShell versions as mechanical translations so agents do not let them drift apart.
- **36. Skip the Terraform/App Runner stretch goal** unless it is being taught. It adds a cloud account, IAM, and a registry to a project whose whole pitch is "one Docker command".
- **37. Consider dropping `user_id` from the LLM/chat path.** The forward-compatibility argument for `user_id` on the data tables is cheap and fine, but the hardcoded `"default"` threading through every function signature is noise. A module-level `DEFAULT_USER_ID` constant used at the query layer, rather than a parameter on every function, keeps the schema future-proof without the ceremony.

### 13.5 Nits

- **38.** `/api/health` should report the active market data source (`simulator` | `massive`) and whether the price cache is populated — that single field will answer most "why is nothing moving?" questions.
- **39.** The seeded watchlist (10 tickers, Section 7) and `seed_prices.py` are two sources of truth for the same list. Have the DB seed import the ticker list from the market module.
- **40.** Section 6 says SSE pushes "for all tickers known to the system"; `stream.py` sends the **entire** price map on every change, not a delta. At 10-20 tickers that is fine — worth one sentence confirming it is deliberate so nobody "optimizes" it into a delta protocol.

### 13.6 Second pass — open questions after triage

*Added after review triage. Item 30 (drop the Playwright container) was rejected: the containerized E2E rig stays. Items 8–15 remain unanswered and will be built to the recommendations stated there unless decided otherwise; 12 and 14 are product calls that should not be defaulted.*

**41. Keeping the Playwright container has three unstated consequences.** Now that `test/docker-compose.test.yml` is confirmed, the spec should pin: (a) tests address the app by **service name** — `http://app:8000`, not `localhost`; (b) the compose file sets `LLM_MOCK=true` on the app service; (c) E2E runs against the **production image**, so CI must build it first — a full frontend build per test run is the real cost of this choice. Note this makes item 34 cleaner rather than contradicting it: drop the root `docker-compose.yml` and the test compose file becomes the only one in the repo, with no ambiguity about which to use.

**42. What is total portfolio value when a price is missing?** The price cache is cold for the first ~500ms after boot, and in Massive mode a rejected symbol may never get a price at all. `cash + Σ(qty × current_price)` is undefined in both cases.
**Recommendation:** fall back to `avg_cost` for any position with no cached price, and have the 30s snapshot task skip its first run until the cache is populated — otherwise the P&L chart opens with a garbage data point at t=0.

**43. Is there a reset path?** Nothing returns the portfolio to $10,000 except deleting the Docker volume, and a student demoing an AI that auto-executes trades will want one within the first five minutes. Options: a `POST /api/reset` endpoint (~15 lines: truncate positions/trades/snapshots/chat, restore cash) with a small header button, or simply document `docker volume rm finally-data` in the README. **Decision needed** — endpoint or documentation.

**44. What does the LLM actually see?** Section 9's context list is cash, positions with P&L, watchlist with prices, and total value — no trade history and no price history. That makes "how has my portfolio done today?" and "why did you buy NVDA earlier?" both unanswerable, which are among the first things anyone will ask an AI trading assistant. Resolve alongside item 12: if the `trades` table gets an endpoint, the chat context is its main consumer. Also decide whether recent portfolio snapshots go into the prompt so the assistant can discuss trajectory rather than only the current state.
**Decided:** both. Recent trades and a downsampled snapshot trajectory are included in the chat context — shape and limits in `planning/API_CONTRACT.md` §6.

**45. `LLM_MOCK=true` needs a defined response shape.** The E2E scenario asserts that "trade execution appears inline", so a single canned string will not do — the mock must branch on the user message (e.g. a message containing "buy" returns a `trades` array; one containing "watch" returns a `watchlist_changes` array; anything else returns message-only). This is a contract between the backend agent and the test agent and belongs in this document, not in whichever one gets written first.

**46. One charting library or two?** Section 10 offers "Lightweight Charts or Recharts" as if interchangeable. They are not: Lightweight Charts is canvas-based and purpose-built for price series but has **no treemap**; Recharts is SVG-based and does have a `Treemap`. Taken literally the plan needs both, plus a third approach for sparklines.
**Recommendation:** standardize on **Recharts alone** — at 10–20 tickers the performance argument for canvas does not bite, and one library beats two. Sparklines as tiny inline SVG. Say so explicitly, or the frontend agent will decide silently and the bundle will carry both.

**47. Does the header's live total value compute client-side or poll?** "Portfolio total value (updating live)" can mean recomputing from the SSE price stream against held quantities, or polling `GET /api/portfolio` on a timer.
**Recommendation:** compute client-side from SSE prices × positions, and refetch positions only after a trade or a chat action. Left unstated, an agent will build a 1-second poll.

**48. Freeze the API contract before parallel work starts.** The frontend and backend agents both depend on shapes that are currently scattered across Section 8 and items 22, 23, and 24 — and several are still undefined. A `planning/API_CONTRACT.md` (request/response bodies for every endpoint, the SSE payload, and the `actions` JSON) should be the first deliverable of the next phase, so the two agents can work against it rather than against each other.
**Status:** drafted at `planning/API_CONTRACT.md` — decisions 42, 45 and 47 are resolved there; 12, 14, 43, 44 and 46 remain open and are listed in its final section.
