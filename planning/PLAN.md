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


| Decision                | Rationale                                                                                     |
| ----------------------- | --------------------------------------------------------------------------------------------- |
| SSE over WebSockets     | One-way push is all we need; simpler, no bidirectional complexity, universal browser support  |
| Static Next.js export   | Single origin, no CORS issues, one port, one container, simple deployment                     |
| SQLite over Postgres    | No auth = no multi-user = no need for a database server; self-contained, zero config          |
| Single Docker container | Students run one command; no docker-compose for production, no service orchestration          |
| uv for Python           | Fast, modern Python project management; reproducible lockfile; what students should learn     |
| Market orders only      | Eliminates order book, limit order logic, partial fills — dramatically simpler portfolio math |


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

- `frontend/` is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- `backend/` is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- `backend/db/` contains schema SQL definitions and seed logic. The backend lazily initializes the database on first request — creating tables and seeding default data if the SQLite file doesn't exist or is empty.
- `db/` at the top level is the runtime volume mount point. The SQLite file (`db/finally.db`) is created here by the backend and persists across container restarts via Docker volume.
- `planning/` contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- `test/` contains Playwright E2E tests and supporting infrastructure (e.g., `docker-compose.test.yml`). Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- `scripts/` contains start/stop scripts that wrap Docker commands.

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


| Method | Path                 | Description                      |
| ------ | -------------------- | -------------------------------- |
| GET    | `/api/stream/prices` | SSE stream of live price updates |




### Portfolio


| Method | Path                     | Description                                                  |
| ------ | ------------------------ | ------------------------------------------------------------ |
| GET    | `/api/portfolio`         | Current positions, cash balance, total value, unrealized P&L |
| POST   | `/api/portfolio/trade`   | Execute a trade: `{ticker, quantity, side}`                  |
| GET    | `/api/portfolio/history` | Portfolio value snapshots over time (for P&L chart)          |




### Watchlist


| Method | Path                      | Description                                  |
| ------ | ------------------------- | -------------------------------------------- |
| GET    | `/api/watchlist`          | Current watchlist tickers with latest prices |
| POST   | `/api/watchlist`          | Add a ticker: `{ticker}`                     |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker                              |




### Chat


| Method | Path        | Description                                                                 |
| ------ | ----------- | --------------------------------------------------------------------------- |
| GET    | `/api/chat` | Recent conversation history (messages with their `actions`), oldest first    |
| POST   | `/api/chat` | Send a message, receive complete JSON response (message + executed actions) |

The frontend calls `GET /api/chat` on page load so the conversation survives a
refresh. It returns the same message shape as `POST /api/chat` so the frontend
renders history and live replies through one code path.




### System


| Method | Path          | Description                          |
| ------ | ------------- | ------------------------------------ |
| GET    | `/api/health` | Health check (for Docker/deployment) |


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

`scripts/start_mac.sh` (macOS/Linux):

- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container with the volume mount, port mapping, and `.env` file
- Prints the URL to access the app
- Optionally opens the browser

`scripts/stop_mac.sh` (macOS/Linux):

- Stops and removes the running container
- Does NOT remove the volume (data persists)

`scripts/start_windows.ps1` / `scripts/stop_windows.ps1`: PowerShell equivalents for Windows.

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



## 13. Review Notes — Questions, Clarifications, Simplifications

Added by a documentation review pass, cross-checked against the completed market
data code in `backend/app/market/`. Items are grouped by urgency: **A** should be
decided before the relevant agent starts, **B** are smaller clarifications, **C**
are optional simplifications.

### A. Contract gaps that block an agent

**A1. "Daily change %" has no source of truth.**
Section 10 asks the watchlist to show a daily change %, but the implemented
`PriceUpdate` (`backend/app/market/models.py`) only carries `previous_price` —
the price from the previous tick, ~500ms ago. There is no session-open or
prior-close reference price in the cache, the SSE payload, or the database.
Decide one of:

- (a) Frontend baseline: the first price seen since page load is the reference,
and the column is relabelled "change since session start".
- (b) Backend adds an `open_price` per ticker to the cache (seeded at startup for
the simulator, taken from the Massive daily bar for real data) and includes it
in the SSE payload.
Option (a) is honest and needs no backend change; option (b) is more realistic
but only meaningful with real data. This needs a decision before the Frontend
Engineer builds the watchlist.

**A2. What is the tracked ticker set?**
Section 6 says the price source tracks "all tickers known to the system — in the
single-user model this is equivalent to the user's watchlist". That is not quite
true: a user can hold a position in a ticker they later remove from the
watchlist, and portfolio valuation would then have no price. Proposed rule to
state explicitly: tracked = watchlist union tickers with a non-zero position, and
removing a ticker from the watchlist never stops tracking it while a position is
open.

**A3. Unknown tickers.**
`POST /api/watchlist {ticker}` and the LLM's `watchlist_changes` can name any
symbol. The simulator only has seed prices for a fixed list
(`backend/app/market/seed_prices.py`). What happens for PYPL (real but unseeded)
or ZZZZ (not a real symbol)? Options: reject anything outside a known symbol
list, or accept any 1-5 letter uppercase symbol and assign it a default seed
price and GBM params. The plan's own example in section 9 adds PYPL, so the
second option is implied but never stated. Also define the rejection response
(400 with what body?) so the frontend and the chat error path can render it.

**A4. Trading a ticker with no cached price.**
`POST /api/portfolio/trade` fills instantly "at current price". Define the
behaviour when the cache holds no price for that ticker — not tracked yet, or
within the first tick after startup. Suggested: reject with a clear error, and
have the trade endpoint add a valid symbol to tracking first.

**A5. SSE payload shape is under-specified and already diverges from the build.**
Section 6 says "each SSE event contains ticker, price, previous price, timestamp,
and change direction", which reads as one event per ticker. The implementation
(`backend/app/market/stream.py`) sends a single event whose data is a dict keyed
by ticker, and pushes only when the cache version changes rather than
unconditionally every 500ms. Update section 6 to match what was built, and pin
down for the frontend: the event name (unnamed `message`?), whether a keepalive
comment is sent during quiet periods, and the `retry:` interval.

**A6. Bootstrap data on page load.**
There is no endpoint to fetch chat history, so a page refresh loses the visible
conversation even though `chat_messages` persists it. Similarly, the main chart
in section 10 has no historical price endpoint — confirm it too is accumulated
from SSE since page load and will be empty on first render, or add
`GET /api/prices/{ticker}/history`.

**DECIDED (chat history):** a refresh must not lose the conversation. `GET
/api/chat` is added to section 8 and the frontend calls it on page load. Since C3
(a single `GET /api/state` bootstrap endpoint) was dropped, the initial load
issues separate calls to `/api/portfolio`, `/api/watchlist`,
`/api/portfolio/history` and `/api/chat`. The window is bounded by B5 — the same
limit used for the LLM prompt applies here. Still open: the main chart's
historical prices.

### B. Smaller clarifications

**B1. Realized P&L.** Positions carry `avg_cost` and the plan defines unrealized
P&L, but nothing defines realized P&L on a sell. State the convention: sells do
not change `avg_cost`, and realized P&L is either not surfaced or is derived from
the `trades` log. If it is not shown anywhere, say so.

**B2. Float money precision.** All monetary columns are SQLite `REAL`. Selling an
entire position through repeated fractional sells can leave a residual quantity
like 1e-15. State a rule: delete the position row when quantity < 1e-6, round
cash to 2dp on write, round quantity to 6dp.

**B3. Trade input validation.** Quantity must be > 0 — reject zero, negative, and
non-numeric. Section 9 states that buys are validated against cash and sells
against held quantity for LLM trades; say explicitly that the manual endpoint
uses the identical validation path.

**B4.** `portfolio_snapshots` **growth and window.** Every 30 seconds is ~2,880 rows
per day, unbounded, and accrues even when nobody has the app open. Specify a
retention rule (keep 24h, or only snapshot while at least one SSE client is
connected) and what window or downsampling `GET /api/portfolio/history` returns.

**B5. Chat history window.** Section 9 says "recent conversation history" without
a number. Specify one (e.g. the last 20 messages) so prompt size is bounded.

**B6. The LLM cannot know its own trade results.** In the single-call flow the
LLM writes `message` before the trades execute, so it cannot report a fill price
or a validation failure in its prose. Section 9 steps 6-7 should state the
resolution: the `actions` array is the authoritative receipt, the frontend
renders it as confirmation chips beneath the message, and failures appear there
rather than in the prose. The alternative — a second LLM call after execution —
doubles latency for little gain.

**B7. Duplicate chat submissions.** Trades auto-execute with no confirmation, so a
double-submitted message executes trades twice. One guard is worth having:
disable the input while a request is in flight, or accept a client-generated
`request_id`.

**B8. Environment variables beyond the three listed.** Nothing covers the LLM
model name, the Massive poll interval, the simulator tick interval, or the port.
Either add them as optional vars with documented defaults, or state plainly that
they are hardcoded constants.

**B9.** `.env` **handling is described two ways.** Section 5 says the backend reads
`.env` from the project root; section 11 passes `--env-file .env` to `docker run`.
Inside the container the project-root `.env` does not exist unless mounted.
Recommend stating: `--env-file` is the only mechanism in Docker, and
`python-dotenv` is a local-dev convenience only.

**B10. Volume path contradiction.** Section 11 shows a named volume
(`-v finally-data:/app/db`) and then says "the `db/` directory in the project
root maps to `/app/db`". Those are different things. Pick one — a bind mount
(`-v ./db:/app/db`) is easier for students to inspect, a named volume is tidier.
Also note that `db/.gitkeep` from the section 4 tree does not exist in the repo
yet.

**B11. Watchlist ordering.** There is no `sort_order` column, so UI order is
presumably `added_at` ascending. State it, otherwise the seeded ten will render
in an order that looks arbitrary.

**B12. Colour tokens for price movement.** The palette in section 2 gives three
brand colours but no green or red, which are the most-used colours in the UI. Add
explicit up/down hex values plus the flash background variants.

**B13. Market hours.** The simulator runs 24/7; Massive returns stale or closed
prices outside US market hours, so the demo goes flat. One sentence on expected
behaviour is enough, even if it is "accepted, no special handling".

**B14. Health check content.** `GET /api/health` could cheaply report the active
market data source and whether the price cache has been populated, giving E2E
tests a reliable readiness gate instead of polling the UI.

**B15. Deployment and the absent auth model.** "No login" is right for local use,
but the optional cloud deployment in section 11 means one shared `default`
portfolio that any visitor can trade, backed by the owner's OpenRouter key. Add a
warning line so nobody deploys it publicly without understanding that.

### C. Opportunities to simplify

**C1. Three ways to launch the app.** Section 11 specifies four shell scripts plus
an optional `docker-compose.yml`, and section 12 adds a third
`docker-compose.test.yml`. Consider keeping `docker-compose.yml` as the single
supported path with the scripts as thin wrappers over it, or dropping the scripts
entirely. Related: `start_mac.sh` also runs on Linux, so `start.sh` / `start.ps1`
are more accurate names.

**C2. Run Playwright from the host, not a container.** The Playwright container  
plus a second compose file exists only to avoid installing browsers. For this  
project, `npx playwright test` on the host against `http://localhost:8000`  
removes a file and an image with no loss of coverage.

**C5. Snapshot on demand rather than on a timer.** If B4 is resolved by
snapshotting only after trades and while a client is connected, the 30-second
background task disappears entirely, along with a source of test flakiness.

**C6. Next.js may be more than is needed.** With `output: 'export'` there is no
SSR, no API routes and no server components — the build is a static SPA. Vite
plus React produces the same artifact with a faster, simpler Dockerfile stage.
Only worth changing if the frontend has not started; if Next.js is a deliberate
teaching choice, say so here so the question stops recurring.

**C7. Reconsider the SSE reconnection E2E test.** "Disconnect and verify
reconnection" requires driving CDP network conditions and is the flakiest item in
the list for the least coverage — `EventSource` retry is browser behaviour, not
application code. A unit test that the stream generator exits cleanly on client
disconnect covers the part this project actually owns.