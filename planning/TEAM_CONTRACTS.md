# FinAlly — Team Contracts

The shared contract for the agent team. `PLAN.md` is the spec; this file pins down **ownership, module boundaries and interfaces** so six engineers can build in parallel. If you need a contract change, append a note to the **Change Log** at the bottom and message the affected owner — do not silently diverge.

## 1. Team & File Ownership

Only edit files you own. Need something in someone else's area? Message them (or the lead), or log it in `planning/ISSUES.md`.

| Role | Name | Owns |
|---|---|---|
| Database Engineer | `db-engineer` | `backend/app/db/**`, `backend/tests/db/**` |
| Backend API Engineer | `backend-engineer` | `backend/app/main.py`, `backend/app/config.py`, `backend/app/services/**`, `backend/app/routes/**` (except `chat.py`), `backend/tests/api/**`, `backend/tests/services/**`, `backend/pyproject.toml` deps (others may `uv add` their own deps) |
| LLM Engineer | `llm-engineer` | `backend/app/llm/**`, `backend/app/routes/chat.py`, `backend/tests/llm/**` |
| Frontend Engineer | `frontend-engineer` | `frontend/**` |
| DevOps Engineer | `devops-engineer` | `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `scripts/**`, `.env.example`, `db/.gitkeep`, `.gitignore`, root `README.md` run instructions |
| Integration Tester | `integration-tester` | `test/**` (Playwright + `test/docker-compose.test.yml`), `planning/ISSUES.md` |

`backend/app/market/**` is finished — treat as read-only (see `backend/CLAUDE.md`).

**Git:** everyone works in the same checkout on branch `agents-teams`. Do **not** commit, stash, reset, checkout or rebase — the lead commits. Do not edit files outside your area.

## 2. Backend Layout

```
backend/app/
├── main.py            # backend-engineer: FastAPI app, lifespan, router wiring, static files
├── config.py          # backend-engineer: env loading (.env from repo root), paths
├── market/            # DONE — read-only
├── db/                # db-engineer
│   ├── __init__.py    # public API below
│   ├── schema.sql
│   └── ...
├── services/          # backend-engineer: business logic shared by REST routes and LLM
│   ├── portfolio.py
│   └── watchlist.py
├── routes/            # backend-engineer (chat.py = llm-engineer)
│   ├── portfolio.py, watchlist.py, health.py
│   └── chat.py
└── llm/               # llm-engineer
```

(PLAN.md says `backend/db/` for schema; we keep it inside the importable package as `backend/app/db/`.)

## 3. Database API — `app.db` (db-engineer)

Stdlib `sqlite3`, synchronous (calls are sub-millisecond; fine to call from async routes). Rows returned as plain `dict`s. Timestamps are ISO-8601 UTC strings. All functions take `user_id: str = "default"` as a keyword arg. Tickers are stored upper-case.

```python
# connection / lifecycle
DEFAULT_USER = "default"
def get_db_path() -> Path            # env FINALLY_DB_PATH, else <repo-root>/db/finally.db
def init_db(path: Path | None = None) -> None   # idempotent: create dir, tables, seed if empty
def get_connection() -> sqlite3.Connection      # row_factory set, foreign keys on, WAL mode
@contextmanager
def transaction() -> Iterator[sqlite3.Connection]   # BEGIN IMMEDIATE ... COMMIT/ROLLBACK
# Every function below accepts optional conn: sqlite3.Connection | None = None so
# callers can compose several writes in one transaction().

# users_profile
def get_cash_balance(*, user_id=..., conn=None) -> float
def set_cash_balance(amount: float, *, user_id=..., conn=None) -> None

# watchlist
def get_watchlist(*, user_id=..., conn=None) -> list[str]          # ordered by added_at
def add_watchlist_ticker(ticker, *, user_id=..., conn=None) -> bool  # False if already present
def remove_watchlist_ticker(ticker, *, user_id=..., conn=None) -> bool  # False if absent

# positions
def get_positions(*, user_id=..., conn=None) -> list[dict]   # {ticker, quantity, avg_cost, updated_at}
def get_position(ticker, *, user_id=..., conn=None) -> dict | None
def upsert_position(ticker, quantity, avg_cost, *, user_id=..., conn=None) -> None
def delete_position(ticker, *, user_id=..., conn=None) -> None

# trades
def record_trade(ticker, side, quantity, price, *, user_id=..., conn=None) -> dict  # returns row incl. id, executed_at
def get_trades(*, limit=100, user_id=..., conn=None) -> list[dict]   # newest first

# portfolio_snapshots
def record_snapshot(total_value, *, user_id=..., conn=None) -> dict
def get_snapshots(*, limit=1000, user_id=..., conn=None) -> list[dict]  # oldest first: {total_value, recorded_at}

# chat_messages
def add_chat_message(role, content, actions: dict | list | None = None, *, user_id=..., conn=None) -> dict
def get_chat_messages(*, limit=50, user_id=..., conn=None) -> list[dict]  # oldest first; actions parsed from JSON
```

Seed: profile `default` with `10000.0` cash; watchlist AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX. Tests must use a temp DB via `FINALLY_DB_PATH` / `init_db(tmp_path / "test.db")`.

## 4. Service Layer — `app.services` (backend-engineer)

Shared by REST routes **and** the LLM engineer's chat flow, so trade validation is identical.

```python
# app/services/portfolio.py
class TradeError(Exception): ...          # message is user-facing ("Insufficient cash: ...")
def execute_trade(ticker: str, side: str, quantity: float, price_cache: PriceCache) -> dict
    # validates (side in buy/sell, quantity > 0, price available, cash / shares sufficient),
    # atomically updates cash + position + trade log, records a snapshot.
    # Returns the trade dict {id, ticker, side, quantity, price, executed_at}. Raises TradeError.
def get_portfolio(price_cache: PriceCache) -> dict      # shape = GET /api/portfolio response
def record_portfolio_snapshot(price_cache: PriceCache) -> dict

# app/services/watchlist.py
async def add_ticker(ticker: str, source: MarketDataSource) -> dict   # raises ValueError on invalid ticker
async def remove_ticker(ticker: str, source: MarketDataSource) -> bool
def get_watchlist_with_prices(price_cache: PriceCache) -> list[dict]
async def sync_tracked_tickers(source: MarketDataSource) -> None  # source.set_tickers(watchlist ∪ held positions)
```

After every trade, callers (route or LLM) must `await sync_tracked_tickers(source)` (a sold-out position not on the watchlist stops being tracked).

**App state** (set in `main.py` lifespan): `app.state.price_cache: PriceCache`, `app.state.market_source: MarketDataSource`. Routes read them via `request.app.state`. The lifespan also runs `init_db()`, starts the market source with watchlist ∪ positions (seeding `initial_prices` from position avg costs), and runs a background task that records a snapshot every 30 s.

**Static files:** `main.py` serves the Next.js export from env `FINALLY_STATIC_DIR` (default `backend/static`) at `/`, with `index.html` fallback, mounted **after** all `/api` routers. If the directory doesn't exist, skip the mount (dev mode).

**Ticker validation:** `^[A-Z][A-Z0-9.\-]{0,9}$` after `normalize_ticker`.

## 5. REST API (backend-engineer; chat by llm-engineer)

All JSON. Errors use FastAPI's `{"detail": "<message>"}` with the status codes listed. Numbers are floats; money rounded to 2 dp in responses.

**`GET /api/health`** → `200 {"status": "ok"}`

**`GET /api/portfolio`** →
```json
{"cash_balance": 8100.0, "positions_value": 1950.0, "total_value": 10050.0,
 "unrealized_pnl": 50.0, "unrealized_pnl_percent": 2.63,
 "positions": [{"ticker": "AAPL", "quantity": 10, "avg_cost": 190.0, "current_price": 195.0,
                "market_value": 1950.0, "unrealized_pnl": 50.0, "unrealized_pnl_percent": 2.63,
                "weight": 0.194}]}
```
`current_price` falls back to `avg_cost` if the cache has no price yet.

**`POST /api/portfolio/trade`** body `{"ticker": "AAPL", "quantity": 10, "side": "buy"}` →
`200 {"trade": {id, ticker, side, quantity, price, executed_at}, "portfolio": <GET /api/portfolio shape>}` ·
`400` validation / insufficient cash / insufficient shares / no price available.

**`GET /api/portfolio/history`** → `200 {"snapshots": [{"total_value": 10000.0, "recorded_at": "..."}]}` (oldest first)

**`GET /api/watchlist`** →
`200 {"tickers": [{"ticker": "AAPL", "price": 190.1, "previous_price": 190.0, "change_percent": 0.05, "day_change_percent": 0.4, "direction": "up", "timestamp": 1731400000.0}]}` — price fields `null` if no price yet.

**`POST /api/watchlist`** body `{"ticker": "PYPL"}` → `201 {"ticker": "PYPL", ...same item shape}` · `400` invalid ticker · `409` already in watchlist.

**`DELETE /api/watchlist/{ticker}`** → `200 {"ticker": "PYPL", "removed": true}` · `404` not in watchlist.

**`POST /api/chat`** body `{"message": "Buy 5 NVDA"}` →
```json
{"message": "Done — bought 5 NVDA at $880.12.",
 "trades": [{"ticker": "NVDA", "side": "buy", "quantity": 5, "status": "executed",
             "price": 880.12, "error": null}],
 "watchlist_changes": [{"ticker": "PYPL", "action": "add", "status": "executed", "error": null}]}
```
`status` is `"executed"` or `"failed"` (with `error` message). `400` on empty message. LLM/network failure → `200` with an apologetic `message` and empty actions (never a 500 to the UI).

**`GET /api/chat/history`** → `200 {"messages": [{"id", "role", "content", "actions", "created_at"}]}` (oldest first; `actions` has the `trades`/`watchlist_changes` shape above for assistant messages). Lets the chat panel restore on reload.

**`GET /api/stream/prices`** (existing, `app/market/stream.py`) — SSE; each `data:` frame is the **full map** `{ "AAPL": {ticker, price, previous_price, timestamp, change, change_percent, direction, reference_price, day_change, day_change_percent}, ... }`. Emitted when the cache changes (checked every 500 ms), `retry: 1000`, `: keepalive` comments.

## 6. LLM (llm-engineer)

- Follow `.claude/skills/cerebras/SKILL.md`: LiteLLM → `openrouter/openai/gpt-oss-120b`, `extra_body={"provider": {"order": ["cerebras"]}}`, `reasoning_effort="low"`, Pydantic structured output. Call `litellm.acompletion` (or run `completion` in a thread) so the event loop is not blocked.
- Entry point: `async def handle_chat(message: str, price_cache: PriceCache, source: MarketDataSource) -> dict` returning the `/api/chat` response shape. It loads context (portfolio, watchlist with prices), last ~20 chat messages, calls the LLM (or mock), executes trades via `app.services.portfolio.execute_trade` and watchlist changes via `app.services.watchlist`, calls `sync_tracked_tickers`, stores user + assistant messages via `app.db.add_chat_message`.
- `LLM_MOCK=true` → deterministic responses, no network. Mock rules the E2E tests rely on (case-insensitive):
  - message contains `buy <N> <TICKER>` → trade buy N TICKER, reply starts with `"Mock: buying"`
  - `sell <N> <TICKER>` → sell trade
  - `add <TICKER>` / `watch <TICKER>` → watchlist add; `remove <TICKER>` → watchlist remove
  - anything else → `{"message": "Mock response: I'm FinAlly, your AI trading assistant.", "trades": [], "watchlist_changes": []}`
- `app/routes/chat.py` exposes `router = APIRouter(prefix="/api/chat")` with `POST ""` and `GET "/history"`; backend-engineer includes it in `main.py`.

## 7. Frontend (frontend-engineer)

- Next.js + TypeScript + Tailwind, `output: 'export'`, build output in `frontend/out/` (DevOps copies it to `backend/static/` in the image).
- All calls are same-origin `/api/*`. For local dev, configure `next.config` rewrites/proxy to `http://localhost:8000` only in dev (rewrites don't apply to export — guard by `NODE_ENV`).
- **Stable `data-testid`s for E2E** (integration-tester relies on these):
  `header-total-value`, `header-cash`, `connection-status` (with `data-status="connected|reconnecting|disconnected"`),
  `watchlist`, `watchlist-row-<TICKER>`, `watchlist-price-<TICKER>`, `watchlist-remove-<TICKER>`, `watchlist-add-input`, `watchlist-add-button`,
  `main-chart`, `selected-ticker`,
  `trade-ticker-input`, `trade-quantity-input`, `trade-buy-button`, `trade-sell-button`, `trade-error`,
  `positions-table`, `position-row-<TICKER>`,
  `portfolio-heatmap`, `pnl-chart`,
  `chat-panel`, `chat-input`, `chat-send-button`, `chat-message` (each message; add `data-role="user|assistant"`), `chat-loading`, `chat-action` (each inline trade/watchlist confirmation).

## 8. Docker & Runtime (devops-engineer)

- Multi-stage Dockerfile per PLAN.md §11: Node 20 slim builds `frontend/` → `out/`; Python 3.12 slim + uv, `uv sync --frozen --no-dev`, copy `out/` to `/app/backend/static`.
- Container env: `FINALLY_DB_PATH=/app/db/finally.db`, `FINALLY_STATIC_DIR=/app/backend/static`. Volume `finally-data:/app/db`. Port 8000. CMD `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000` (from `/app/backend`). Healthcheck on `/api/health`.
- Container name `finally`, image `finally`.

## 9. Testing

- Backend: `cd backend && uv run --extra dev pytest`, `uv run --extra dev ruff check app tests`. Each engineer writes tests for their own area; the whole suite must stay green.
- Frontend: Vitest/Jest + React Testing Library, `npm test`.
- E2E: `test/` Playwright, `test/docker-compose.test.yml` (app with `LLM_MOCK=true` + Playwright container). Issues go to `planning/ISSUES.md` with owner, repro, expected vs actual; owners mark them fixed.

## Change Log

- 2026-11-12 — initial contract (lead).
- 2026-11-12 — §8: container CMD runs uvicorn directly from the image venv (`/app/backend/.venv/bin/uvicorn`) instead of `uv run`, so no network/sync at runtime (devops-engineer). Frontend must commit `frontend/package-lock.json` (Dockerfile uses `npm ci`).
