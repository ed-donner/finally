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
│   └── stop_mac.sh           # Stop Docker container (macOS/Linux)
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

# Optional: Massive API polling cadence in seconds (default: 15, safe for the free tier)
MASSIVE_POLL_SECONDS=15

# Optional: Set to "true" for deterministic mock LLM responses (testing)
LLM_MOCK=false

# Optional: Integer seed for the simulator's RNG. When set, GBM moves and shock events
# are reproducible — used by E2E tests to make price-driven assertions stable.
SIMULATOR_SEED=
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
- Polls for the union of all watched tickers
- Poll cadence is controlled by `MASSIVE_POLL_SECONDS` (default `15` — safe for the free tier; lower for paid tiers)
- Parses REST response into the same format as the simulator

### Shared Price Cache

- A single background task (simulator or Massive poller) writes to an in-memory price cache
- The cache holds the latest price, previous price, and timestamp for each ticker
- SSE streams read from this cache and push updates to connected clients
- The cache tracks the **union of watched and held tickers**. A position whose ticker is no longer on the watchlist (e.g., user deleted it after buying) still needs a live price for portfolio valuation, so the backend keeps it in the cache as long as `quantity > 0`.
- `DELETE /api/watchlist/{ticker}` on a currently-held ticker is allowed — the row is removed from `watchlist`, the ticker disappears from the watchlist UI, but it stays in the price cache and SSE stream because the position still references it. Selling the position to 0 quantity is what removes it from tracking.
- This architecture supports future multi-user scenarios without changes to the data layer

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- The server polls the price cache every ~500ms and emits an event only when the cache version changes (i.e., when any tracked ticker ticked). Default event name `message`.
- Each event's `data:` payload is a single JSON object keyed by ticker, where each value is the full `PriceUpdate.to_dict()` from `backend/app/market/models.py`:

  ```json
  {
    "AAPL": {
      "ticker": "AAPL",
      "price": 190.52,
      "previous_price": 190.48,
      "timestamp": 1734040000.0,
      "change": 0.04,
      "change_percent": 0.021,
      "direction": "up"
    },
    "GOOGL": { ... }
  }
  ```

- All currently tracked tickers are included in every event (not just the ones that changed this tick), so the client always has a complete snapshot. Frontends should reconcile by ticker.
- The connection emits a `retry: 1000` directive on connect so `EventSource` auto-reconnects after ~1s on drop.
- No explicit heartbeat is emitted; acceptable for the localhost/demo deployment target. If deployed behind a proxy that closes idle connections, add a periodic `: ping` comment line.

---

## 7. Database

### SQLite with Lazy Initialization

The backend checks for the SQLite database on startup (or first request). If the file doesn't exist or tables are missing, it creates the schema and seeds default data. This means:

- No separate migration step
- No manual database setup
- Fresh Docker volumes start with a clean, seeded database automatically

### Schema

**Scope note: FinAlly is a single-user application.** There is no login, no signup, no session, and no auth middleware. Every row uses `user_id = "default"`. The `user_id` column exists on every table so a future maintainer could add multi-user support without a schema migration, but no multi-user UI, routing, or auth code should be implemented as part of this build.

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

_Retention: append-only, no compaction. ~2,880 rows/day during a 24h run is trivial for SQLite. If a long-running deployment ever becomes an issue, a downsampling job can be added; not in scope for this build._

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON — trades executed and watchlist changes made on this turn; null for user messages)
- `created_at` TEXT (ISO timestamp)

### Default Seed Data

- One user profile: `id="default"`, `cash_balance=10000.0`
- Ten watchlist entries: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX

---

## 8. API Endpoints

### Initial Load
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/state` | One-shot snapshot for app boot: portfolio (cash, positions, P&L, total value) + watchlist (tickers with latest prices). Used by the frontend on mount instead of separate `/api/portfolio` + `/api/watchlist` calls. |

### Market Data
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/stream/prices` | SSE stream of live price updates |

### Portfolio
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/portfolio` | Current positions, cash balance, total value, unrealized P&L |
| POST | `/api/portfolio/trade` | Execute a market trade. Body: `{ticker: string, side: "buy"\|"sell", quantity: number}`. Response on success (200): `{trade: {...}, portfolio: {...}}` — the new trade row plus the updated portfolio snapshot. Response on validation failure (400): `{error: string, code: "INSUFFICIENT_CASH"\|"INSUFFICIENT_SHARES"\|"UNKNOWN_TICKER"\|"INVALID_QUANTITY"}`. |
| GET | `/api/portfolio/history` | Portfolio value snapshots over time (for P&L chart) |

### Watchlist
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Current watchlist tickers with latest prices |
| POST | `/api/watchlist` | Add a ticker. Body: `{ticker: string}`. 201 on success, 409 if already on the watchlist. |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker from the watchlist. 204 on success. Allowed even if the ticker is currently held (see Section 6). |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat` | Send a message. Body: `{message: string}`. Response (200): `{message: string, trades: [...], watchlist_changes: [...], errors: [...], portfolio: {...}}` — the assistant's reply, the executed actions, any validation errors for actions the LLM proposed, and the updated portfolio snapshot so the UI can reconcile without a follow-up GET. |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (used by Docker `HEALTHCHECK`). Returns 200 with `{"status": "ok"}`. |

---

## 9. LLM Integration

When writing code to make calls to LLMs, use cerebras-inference skill to use LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider. Structured Outputs should be used to interpret the results.

There is an OPENROUTER_API_KEY in the .env file in the project root.

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads the last 20 messages from `chat_messages` (10 user + 10 assistant turns at most) — a fixed cap, no token-budget math
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via LiteLLM → OpenRouter, requesting structured output, using the cerebras-inference skill
5. Parses the complete structured JSON response
6. Auto-executes any trades or watchlist changes specified in the response
7. Stores the user and assistant turns in `chat_messages`. The assistant row's `actions` column records the trades and watchlist changes executed on this turn (including any that failed validation, with their error).
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

If a trade fails validation (e.g., insufficient cash), the error appears in the chat response's `errors` array (see Section 8) alongside the trades that did execute. The failed entry is also captured in the assistant message's `actions` JSON so the conversation log is faithful to what was attempted. The frontend surfaces failures inline beneath the assistant message; the LLM does not see the failure on this turn — it sees it next turn when the conversation history is replayed, and can apologize/retry then.

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

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), session change % (vs the first price seen since page load), and a sparkline mini-chart (accumulated from the same SSE stream)
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
- **Chart history is SSE-only.** Both the sparklines and the main ticker chart accumulate points from the SSE stream while the page is open — there is no historical price endpoint. On hard refresh the charts start empty and fill in over the next few minutes. This keeps the simulator and Massive backends symmetric (the simulator has no historical data) and avoids a second data path. The P&L chart is the exception — it reads persisted `portfolio_snapshots` via `/api/portfolio/history`.

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

The SQLite database persists via a host bind-mount onto the project's `db/` directory:

```bash
docker run -v "$(pwd)/db:/app/db" -p 8000:8000 --env-file .env finally
```

The `db/` directory in the project root maps to `/app/db` in the container. The backend writes `finally.db` to this path, where it can be inspected directly from the host (e.g., with `sqlite3 db/finally.db`). The directory is checked into the repo via `.gitkeep`; `db/finally.db` itself is gitignored.

### Start/Stop Scripts

**`scripts/start_mac.sh`** (macOS/Linux):
- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container with the volume mount, port mapping, and `.env` file
- Prints the URL to access the app
- Optionally opens the browser

**`scripts/stop_mac.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT remove the volume (data persists)

Windows users run the equivalent `docker run` / `docker stop` commands directly — see the README for the one-liners. No PowerShell scripts are maintained.

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

**Environment**: Tests run with `LLM_MOCK=true` and `SIMULATOR_SEED=<fixed value>` by default. The seed makes simulator price moves reproducible so price-driven assertions (flash effects, sparkline shape, P&L direction) don't flake.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell shares: cash increases, position updates or disappears
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send a message, receive a response, trade execution appears inline
- SSE resilience: disconnect and verify reconnection

---

## 13. Review Feedback

This section captures open questions, inconsistencies, and simplification opportunities surfaced during a review of the plan. Items here are not yet decided — they need an author/owner call before any agent implements them.

### Open Questions / Clarifications

_All flagged questions have been resolved and folded into Sections 5–12. Summary of resolutions:_

- **SSE payload shape** — Section 6: batched JSON object keyed by ticker, full `PriceUpdate.to_dict()` (includes `change`, `change_percent`, `direction`), default `message` event, `retry: 1000` directive, no heartbeat (acceptable for localhost demo).
- **Historical chart data** — Section 10: charts are SSE-only and start empty on refresh; intentional, keeps simulator/Massive symmetric.
- **Held but unwatched tickers** — Section 6: price cache tracks `watched ∪ held`. `DELETE /api/watchlist/{ticker}` on a held ticker is allowed; the position keeps streaming until sold to 0.
- **Trade and chat contracts** — Section 8: request/response bodies, status codes, and error codes are spelled out in the endpoint tables. Both `/api/portfolio/trade` and `/api/chat` return the updated portfolio so the UI doesn't need a follow-up GET.
- **Conversation history budget** — Section 9: fixed cap of 20 messages.
- **Trade failures in chat** — Section 9: surfaced in the response's `errors` array, persisted in the assistant message's `actions` JSON, and shown inline in the chat UI.
- **Health endpoint** — Section 8: `200` with `{"status": "ok"}`.
- **E2E determinism** — Sections 5 + 12: `SIMULATOR_SEED` env var; tests pin a fixed value.
- **Portfolio snapshot retention** — Section 7: append-only, no compaction; trivial volume.
- **Daily change %** — already resolved as session change % (Section 10).
- **Massive poll cadence** — already resolved as `MASSIVE_POLL_SECONDS` (Sections 5 + 6).

This subsection is intentionally retained as a record; future reviews can add new questions here.

### Inconsistencies

_All flagged inconsistencies have been resolved in Sections 3–11. This subsection is intentionally empty; future reviews can add new entries here._

### Opportunities to Simplify

_All accepted simplifications have been folded into Sections 3–11. This subsection is intentionally empty; future reviews can add new candidates here._
