# FinAlly Team Contract

This is the shared coordination document for the agent team building FinAlly.
**Read this before you start. It is the source of truth for boundaries and interfaces.**
`PLAN.md` remains the full spec; this file pins down ownership, internal interfaces,
and exact wire formats so we can work in parallel without colliding.

The market-data subsystem (`backend/app/market/`) is **already complete** — do not modify it.
Consume it via its public API (see `backend/CLAUDE.md`).

---

## 1. File Ownership (do not edit files outside your lane)

| Agent | Owns (create/edit only these) |
|-------|------------------------------|
| **Database** | `backend/app/db/` (new package), `backend/db/` (schema.sql, seed), `backend/tests/db/` |
| **Backend API** | `backend/app/api/` (routers), `backend/app/services/` (portfolio/trade logic), `backend/app/main.py`, `backend/app/config.py`, `backend/tests/api/`, `backend/tests/services/`. **Owns `pyproject.toml`** (all dependency additions funnel through you). |
| **LLM** | `backend/app/llm/` (new package: client, prompts, schema, mock), `backend/tests/llm/`. Chat endpoint router lives in `backend/app/api/chat.py` — you write it, but coordinate wiring with Backend API. |
| **Frontend** | `frontend/` (entire Next.js project). Nothing else. |
| **DevOps** | `Dockerfile`, `docker-compose.yml`, `.env.example`, `scripts/`, `test/docker-compose.test.yml`, `.dockerignore`. Do NOT touch app source. |
| **Integration Tester** | `test/` (Playwright specs, config, package.json). Reports bugs back; does not fix app code. |

**Shared-file rule:** only Backend API edits `pyproject.toml` and `main.py`. If DB or LLM need a
dependency added, note it in `planning/TEAM_LOG.md` and Backend API adds it.

Append short status notes / cross-team asks to `planning/TEAM_LOG.md` (create it; one line per note, newest at bottom).

---

## 2. Backend internal Python interfaces (the contract between DB / API / LLM)

The **Database** engineer must expose this exact surface from `backend/app/db/` so API and LLM can build against it before it's finished:

```python
# backend/app/db/__init__.py must export:
from app.db import (
    init_db,            # (db_path: str | None = None) -> None. Lazy: create schema + seed if empty. Idempotent.
    get_conn,           # () -> sqlite3.Connection (row_factory = sqlite3.Row). One shared connection or per-call; thread-safe.
)
# Repository functions (module app.db.repo), all take user_id: str = "default":
#   get_profile() -> dict                      # {id, cash_balance, created_at}
#   set_cash_balance(cash: float) -> None
#   get_watchlist() -> list[str]               # tickers, insertion order
#   add_watchlist(ticker: str) -> bool         # False if already present
#   remove_watchlist(ticker: str) -> bool      # False if absent
#   get_positions() -> list[dict]              # [{ticker, quantity, avg_cost, updated_at}]
#   get_position(ticker) -> dict | None
#   upsert_position(ticker, quantity, avg_cost) -> None
#   delete_position(ticker) -> None
#   insert_trade(ticker, side, quantity, price) -> dict   # returns the trade row incl id, executed_at
#   get_trades(limit=100) -> list[dict]
#   insert_snapshot(total_value: float) -> None
#   get_snapshots(limit=500) -> list[dict]     # [{total_value, recorded_at}] ascending by time
#   get_chat_messages(limit=50) -> list[dict]  # ascending; [{role, content, actions, created_at}]
#   insert_chat_message(role, content, actions: dict | None) -> None
```

DB path resolution: read env `FINALLY_DB_PATH`, default `db/finally.db` (repo-root `db/` volume mount).
Store timestamps as ISO-8601 UTC strings. UUIDs via `uuid4().hex`.

**Trade execution** (buy/sell math, validation, snapshot-on-trade) is owned by **Backend API** in
`backend/app/services/portfolio.py`, exposing:

```python
execute_trade(ticker: str, side: str, quantity: float, price: float) -> dict
# Validates: quantity>0; buy needs cash>=quantity*price; sell needs owned>=quantity.
# Updates cash, upserts/deletes position (avg_cost weighted on buys), inserts trade + snapshot.
# Raises TradeError(message) on validation failure. Price comes from PriceCache.
build_portfolio() -> dict   # the /api/portfolio response shape below
```

The **LLM** engineer calls `execute_trade` and the watchlist/db repo functions to auto-apply actions.
Price lookups: `price_cache.get_price(ticker)`. The single shared `PriceCache` + market source are
created in `main.py` lifespan and injected (via `app.state` or a module-level accessor — Backend API decides and documents in TEAM_LOG).

---

## 3. REST API wire formats (exact — Frontend & Integration Tester rely on these)

All JSON, all under `/api`. Money/prices are numbers (floats). Errors: HTTP 4xx with `{"detail": "..."}`.

**GET `/api/health`** → `{"status": "ok"}`

**GET `/api/portfolio`** →
```json
{
  "cash_balance": 10000.0,
  "total_value": 10000.0,
  "positions_value": 0.0,
  "total_unrealized_pl": 0.0,
  "positions": [
    {"ticker":"AAPL","quantity":10,"avg_cost":190.0,"current_price":192.5,
     "market_value":1925.0,"unrealized_pl":25.0,"unrealized_pl_pct":1.32}
  ]
}
```

**POST `/api/portfolio/trade`**  body `{"ticker":"AAPL","quantity":10,"side":"buy"}`
→ `{"success":true,"trade":{...},"portfolio":{<portfolio shape>}}` ; on failure HTTP 400 `{"detail":"Insufficient cash"}`.

**GET `/api/portfolio/history`** → `{"snapshots":[{"total_value":10000.0,"recorded_at":"..."}]}`

**GET `/api/watchlist`** →
```json
{"watchlist":[{"ticker":"AAPL","price":192.5,"previous_price":192.0,"change":0.5,"change_percent":0.26,"direction":"up"}]}
```
(price fields null if no tick yet.)

**POST `/api/watchlist`** body `{"ticker":"PYPL"}` → `{"watchlist":[...]}` (full list). 400 if already present/invalid.
**DELETE `/api/watchlist/{ticker}`** → `{"watchlist":[...]}`. Adds/removes from live market source too.

**POST `/api/chat`** body `{"message":"buy 5 AAPL"}` →
```json
{"message":"Bought 5 shares of AAPL...","trades":[{"ticker":"AAPL","side":"buy","quantity":5,"status":"executed","error":null}],
 "watchlist_changes":[{"ticker":"PYPL","action":"add","status":"applied"}],
 "portfolio":{<portfolio shape>}}
```

**GET `/api/stream/prices`** — already built (SSE). Each event `data:` is JSON:
`{"ticker","price","previous_price","timestamp","change","direction"}` (from `PriceUpdate.to_dict()`).

---

## 4. LLM behavior & mock (Integration Tester depends on this)

- Real mode: LiteLLM → OpenRouter, model `openrouter/openai/gpt-oss-120b`, provider Cerebras, structured outputs. Use the **cerebras-inference** skill. Deps: `litellm`, `pydantic` (tell Backend API to add).
- Structured schema per PLAN §9: `{message, trades[], watchlist_changes[]}`.
- **`LLM_MOCK=true`** → deterministic, no network. Mock rules the Integration Tester will assert on:
  - Message containing `buy N TICKER` (e.g. "buy 5 AAPL") → executes that buy, echoes confirmation.
  - Message containing `sell N TICKER` → executes that sell.
  - Message containing `add TICKER` / `remove TICKER` → watchlist change.
  - Anything else → a canned analytical reply, no actions.
- Auto-execute trades/watchlist changes through `services.portfolio.execute_trade` and db repo; include per-action `status`/`error` so failures surface in the reply.

---

## 5. Config / env

- `OPENROUTER_API_KEY` (real LLM), `MASSIVE_API_KEY` (optional real data), `LLM_MOCK` (default false),
  `FINALLY_DB_PATH` (default `db/finally.db`). `.env` at repo root, gitignored; DevOps writes `.env.example`.
- Backend loads `.env` (Backend API adds `python-dotenv` and centralizes in `app/config.py`).
- **Note:** no `.env` currently exists. Development/tests must work with `LLM_MOCK=true` and the simulator (no keys needed).

## 6. Conventions & build order

- Backend: `uv` project in `backend/`. Test `uv run --extra dev pytest`; lint `uv run --extra dev ruff check`.
- Frontend: Next.js + TS, **`output: 'export'`**, Tailwind dark theme, `EventSource` for SSE, all calls same-origin `/api/*`. In production FastAPI serves the static export; in dev use Next dev server proxying to backend (Frontend decides, documents run steps in `frontend/README.md`).
- Colors: accent `#ecad0a`, blue `#209dd7`, purple `#753991` (submit buttons), bg ~`#0d1117`.
- **Build order:** DB first → API + LLM (consume DB) → Frontend integrates against running backend → DevOps packages → Integration Tester runs E2E with `LLM_MOCK=true`.
- Every engineer writes unit tests for their own code (PLAN §12). Keep the market suite green.
