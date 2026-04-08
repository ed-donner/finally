# FinAlly Project — Comprehensive Review

_Reviewed: 2026-04-08_

---

## Executive Summary

The project is at an early stage. The market data subsystem (one of roughly six major components) is complete and of high quality. Everything else — the FastAPI application entry point, database layer, portfolio/watchlist/chat API, LLM integration, frontend, Docker container, start/stop scripts, and E2E test infrastructure — does not exist yet. The repository has the skeleton of the right structure but is missing the body.

The PLAN.md has been meaningfully updated since the last commit. The changes simplify the design (removing `user_id` columns, dropping the `chat_messages` table, replacing periodic DB snapshots with startup-only snapshots) and add important implementation details (price history cache, LLM error handling spec, SSE scoping clarification). The new plan is internally consistent and correct. Several of the issues identified in prior reviews have been formally resolved in the spec; however, a few pre-existing concerns in the implementation remain open.

---

## 1. PLAN.md Changes — Assessment

### What Changed

| Area | Change | Assessment |
|---|---|---|
| Directory structure | Removed `docker-compose.yml` from the tree | Correct — the plan already describes standalone Docker; a compose file would contradict that |
| SSE scope | "all tickers known to the system" → "all tickers in the watchlist table" | Good clarification; removes ambiguity about which tickers are streamed |
| Schema: `user_id` columns | Removed from all five tables | Correct simplification for a single-user app; eliminates unused complexity |
| Schema: `chat_messages` table | Dropped entirely; conversation held in memory | Correct — in-memory chat history is simpler and sufficient; the stated trade-off (lost on restart) is intentional and documented |
| Schema: `portfolio_snapshots` | Changed from "every 30 seconds by background task + after each trade" to "after each trade + on backend startup" | See concern below — this is a regression for the P&L chart |
| Price history cache | New section added: rolling 200-price deque per ticker, `/api/prices/{ticker}/history` endpoint | Required addition; the previous plan left chart bootstrapping undefined |
| API endpoints | Added `GET /api/prices/{ticker}/history` | Consistent with the new history cache section |
| Watchlist POST | Added "Returns 200 if already in watchlist (idempotent)" | Good — removes a client-side error-handling branch |
| LLM section | Added note on LiteLLM model string format and Cerebras `extra_body` | Clarifies a subtle integration detail that would otherwise cause a runtime error |
| LLM error handling | New section: return HTTP 200 with graceful error message on failure | Good pattern; keeps the frontend error path simple |
| Frontend: watchlist/main chart | Updated to reference `/api/prices/{ticker}/history` for bootstrapping | Consistent with the new history endpoint |
| Frontend: trade bar | Added "fractional shares supported, e.g. 0.5" | Minor but useful for implementers |
| Frontend: P&L chart source | Changed from "portfolio_snapshots" to "`GET /api/portfolio/history`" | Correct — the frontend should reference the API, not the DB table |
| Dockerfile section | Added explicit note that Next.js is a static export | Prevents common mistakes during Docker build implementation |

### Concern: P&L Chart Will Be Nearly Empty Without Periodic Snapshots

The old plan had `portfolio_snapshots` recorded every 30 seconds by a background task. The new plan records only on backend startup and after each trade. If the user opens the app, prices move, but they make no trades, the P&L chart will have a single data point (the startup snapshot). The chart will be a flat horizontal line until the first trade.

More significantly: after the first trade, the chart will show only the startup value and the post-trade value — no indication of how prices moved in between. For a "visually stunning trading workstation" this is a poor user experience.

The simplification is understandable (removes a background task), but the consequences for the P&L chart's usefulness should be acknowledged. The recommended fix is a lightweight periodic snapshot every 60 seconds, only when the user holds any open positions. This was flagged in the prior review and remains a live concern.

---

## 2. Implementation Status

### Market Data Backend (`backend/app/market/`) — Complete

This component is solid and well-engineered. All eight modules are present, lint-clean, and tested at 91% overall coverage (100% on the core modules that matter most).

### Everything Else — Not Started

The following are required by PLAN.md and are entirely absent:

| Component | What's Needed |
|---|---|
| `backend/app/main.py` | FastAPI app entry point, lifespan handler for market data startup/shutdown, static file serving, router registration |
| `backend/app/db/` | Schema SQL, seed data, lazy initialization logic |
| `backend/app/routers/portfolio.py` | `GET /api/portfolio`, `POST /api/portfolio/trade`, `GET /api/portfolio/history` |
| `backend/app/routers/watchlist.py` | `GET /api/watchlist`, `POST /api/watchlist`, `DELETE /api/watchlist/{ticker}` |
| `backend/app/routers/chat.py` | `POST /api/chat` with LLM integration |
| `backend/app/routers/health.py` | `GET /api/health` |
| LLM integration | LiteLLM via OpenRouter/Cerebras with structured output schema |
| `frontend/` | Entire Next.js static export project |
| `Dockerfile` | Multi-stage Node → Python build |
| `scripts/` | `start_mac.sh`, `stop_mac.sh`, `start_windows.ps1`, `stop_windows.ps1` |
| `test/` | Playwright E2E tests, `docker-compose.test.yml` |
| `db/.gitkeep` | Runtime volume mount point |
| `.env.example` | Template for required environment variables |

---

## 3. Open Bugs and Issues in Existing Code

### Bug — Module-level Router in `stream.py` (Medium)

`stream.py` creates `router = APIRouter(...)` at module scope, then `create_stream_router()` registers the `/prices` route on it via a closure:

```python
router = APIRouter(prefix="/api/stream", tags=["streaming"])

def create_stream_router(price_cache: PriceCache) -> APIRouter:
    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        ...
    return router
```

Calling `create_stream_router()` twice registers the route twice on the same router object. In production this is called once, so there is no runtime failure today. But it will silently cause duplicate route registration if any test or tooling calls the factory more than once. Fix: move `router = APIRouter(...)` inside `create_stream_router()` so each call returns a fresh router.

### Missing Feature — Price History Not in `PriceCache` (High)

PLAN.md section 6 now explicitly specifies "a rolling history of the last 200 prices per ticker" and a `GET /api/prices/{ticker}/history` endpoint. The current `PriceCache` stores only the latest `PriceUpdate` per ticker — no history deque. The history endpoint cannot be built without extending `PriceCache`. This needs to be done before the backend API layer is started.

Suggested addition to `PriceCache`:
- Add `self._history: dict[str, deque[PriceUpdate]] = {}` with `maxlen=200`
- Populate it in `update()`
- Add `get_history(ticker: str) -> list[PriceUpdate]` returning a snapshot copy

### Design Gap — P&L Chart Static Between Trades (Medium)

As noted in the PLAN.md review above, recording portfolio snapshots only on startup and after trades produces a near-empty P&L chart in all but the most active trading sessions. A background task recording a snapshot every 60 seconds when `positions` is non-empty is the minimum viable fix.

### Design Gap — Floating-Point Money (Medium)

The SQLite schema uses `REAL` for `cash_balance`, `quantity`, `avg_cost`, `price`, and `total_value`. After repeated buy/sell cycles, floating-point rounding errors will accumulate (e.g., `100 * 190.12345` will not equal what the user expects). Explicit `round(value, 2)` at every trade execution boundary is required. This is not in the spec and needs to be enforced in implementation.

### Design Gap — Trade Atomicity Unspecified (Medium)

A trade execution touches four tables: `users_profile` (cash deduction/credit), `positions` (upsert), `trades` (append), and `portfolio_snapshots` (append). These four writes must be wrapped in a single SQLite transaction. If any write fails mid-trade the database will be in an inconsistent state. The spec does not state this requirement explicitly; it should be enforced in the implementation.

### Design Gap — Ticker Validation Rules Absent (Medium)

The spec does not define what happens when a trade is requested for a ticker that has no current price in the cache (e.g., the ticker was never added to the watchlist, or the cache has not yet been seeded). Both the manual trade API and the LLM auto-execution path need explicit rules: either reject trades for tickers not in the watchlist, or allow them but require a valid cache entry. The behavior on "ticker not found in cache" must be specified before the trade API is built.

### Code Quality — `PriceCache.version` Lock Omission (Low)

The `version` property reads `self._version` without acquiring `self._lock`. On CPython this is safe due to the GIL (integer reads are atomic), but it is inconsistent with the rest of the class's locking discipline and would be unsafe on other Python implementations. It should use the lock for correctness.

---

## 4. Missing Dependencies in `pyproject.toml`

Currently declared: `fastapi`, `uvicorn[standard]`, `numpy`, `massive`, `rich`.

Still needed for the remaining build:
- `litellm` — LLM calls via OpenRouter
- `python-dotenv` or `pydantic-settings` — reading `.env` file into the process
- `aiosqlite` — async SQLite access (if async DB writes are used; `sqlite3` stdlib suffices for sync)
- `httpx` — HTTP test client for ASGI (`pytest-asyncio` + `httpx` replaces requests for FastAPI tests)

---

## 5. Architecture Observations

### Watchlist-to-Market-Source Wiring Is Not Specified

PLAN.md says the SSE endpoint streams prices "for all tickers in the watchlist table." When the user adds or removes a ticker via `POST /api/watchlist` or `DELETE /api/watchlist/{ticker}`, the code must also call `await source.add_ticker(ticker)` or `await source.remove_ticker(ticker)` on the active market data source. This wiring must happen in the watchlist router or a service layer — it cannot happen in the market data module itself since the market module has no knowledge of the database. The plan does not spell this out; it should be made explicit to avoid the backend being built with the watchlist DB and the price cache out of sync.

### In-Memory Chat History and Restart Semantics

Choosing in-memory conversation history is a clean simplification. The consequence (conversation lost on container restart) is now documented. One thing the plan does not address: if the user refreshes the browser, the frontend loses its local copy of the conversation but the backend still holds the history. The plan should clarify whether the `GET /api/chat` or `POST /api/chat` response should include or expose full conversation history, or whether the frontend is expected to maintain its own copy.

### LLM Error Handling Returns HTTP 200 for All Errors

The plan specifies returning HTTP 200 with a user-facing error message for any LLM failure (network error, rate limit, malformed response). This simplifies the frontend but has a consequence: if the LLM returns a malformed structured output that partially parses (e.g., a `trades` array with one valid and one invalid entry), the plan does not specify whether partial execution should occur or the entire response should be discarded. The implementation should treat any structurally invalid LLM response as a complete failure and not execute any partial trades from it.

### Static Export Constraint Well-Documented

The new Dockerfile section note — "This is intentional and permanent — no SSR, no Node.js runtime, no Next.js API routes" — is a useful guardrail. The `output: 'export'` constraint in `next.config.js` means features like `next/image` with remote URLs, `getServerSideProps`, and middleware are unavailable. The frontend implementer should be aware of this constraint early.

---

## 6. Test Coverage Summary

| Area | Coverage |
|---|---|
| `models.py` | 100% |
| `cache.py` | 100% |
| `interface.py` | 100% |
| `seed_prices.py` | 100% |
| `factory.py` | 100% |
| `simulator.py` | 98% |
| `massive_client.py` | 94% |
| `stream.py` | 33% — no ASGI-level test for the SSE endpoint |
| Database layer | 0% — not built |
| Portfolio API | 0% — not built |
| Watchlist API | 0% — not built |
| Chat/LLM API | 0% — not built |
| Frontend | 0% — not built |
| E2E | 0% — not built |

No test exercises all 10 default tickers together (the full 10x10 Cholesky decomposition path is untested).

---

## 7. Docker and Deployment Readiness

Not ready. No `Dockerfile`, no start/stop scripts, no `frontend/` for the Node build stage, no `.env.example`, no `db/.gitkeep`. None of this is blocking for the current phase of development, but these items should not be deferred past the point where the backend and frontend are individually functional.

---

## 8. Recommended Build Order

1. **Extend `PriceCache`** with rolling 200-price history per ticker (deque) — required before the history endpoint can be built
2. **Build `backend/app/main.py`** with FastAPI lifespan: init cache, start market data source, seed watchlist tickers from DB (or defaults), register routers, serve static files
3. **Build database layer** (`backend/app/db/`) with lazy schema creation, seed data, and an explicit transaction wrapper for trade execution
4. **Build routers in order:** health → watchlist (with market source wiring) → portfolio → chat/LLM
5. **Add periodic portfolio snapshot** background task (every 60 seconds when positions exist) — needed for a useful P&L chart
6. **Fix `stream.py` router bug** — move `router = APIRouter(...)` inside `create_stream_router()`
7. **Add `litellm`, `python-dotenv`, `httpx`** to `pyproject.toml` dependencies before building the LLM and test layers
8. **Build frontend** after the backend is manually testable via `curl` or a REST client
9. **Write Dockerfile** once both frontend and backend are functional standalone
10. **Add SSE ASGI integration test** — the only material coverage gap in the completed market data code
