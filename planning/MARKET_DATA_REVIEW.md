# Market Data — Code Review

Reviewed against: `PLAN.md`, `MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, `MASSIVE_API.md`, `MARKET_DATA_DESIGN.md`
Implementation: `backend/app/market/`
Tests: `backend/tests/market/`

---

## Test Results

```
73 passed in 4.87s
Linting: All checks passed (ruff)
```

**Coverage by module:**

| File | Coverage | Missing lines |
|------|----------|---------------|
| `models.py` | 100% | — |
| `cache.py` | 100% | — |
| `factory.py` | 100% | — |
| `interface.py` | 100% | — |
| `seed_prices.py` | 100% | — |
| `simulator.py` | 98% | 149, 268–269 (exception branch in run loop) |
| `massive_client.py` | 94% | 85–87, 125 (poll loop body, fetch method) |
| `stream.py` | **33%** | 26–48, 62–87 (the entire SSE route and generator) |
| **Total** | **91%** | |

All tests pass and linting is clean. The implementation is well-structured, readable, and the core modules are very well covered. The issues below are all actionable and none require architectural rework.

---

## Issues

### 1. `open_price` is missing entirely — CRITICAL

**PLAN.md §6** is explicit:

> The price cache holds `{price, prev_price, open_price, timestamp, direction}` per ticker. `open_price` is the seed price at session start and is the baseline for "daily change %" calculations on the frontend.

The actual `PriceUpdate` dataclass has no `open_price` field, and `PriceCache.update()` has no `open_price` parameter. The frontend formula `(price - open_price) / open_price * 100` cannot be computed from the SSE stream. `GET /api/watchlist` is also specified to return `open_price` — it cannot without this field.

**Impact:** Daily change % column in the watchlist panel will not work.

**Fix:** Add `open_price: float` to `PriceUpdate`. Update `PriceCache.update()` to accept and store it (set on first update, never overwritten). Seed it in `SimulatorDataSource.start()` and `add_ticker()`. Pass `day.open` / `prev_day.close` from the Massive client.

---

### 2. Field name `previous_price` vs `prev_price` — CRITICAL

**PLAN.md §6** pins the SSE event field names:

```json
{"ticker": "AAPL", "price": 191.50, "prev_price": 191.32, "timestamp": "...", "direction": "up"}
```

The implementation uses `previous_price` throughout (`PriceUpdate`, `PriceCache`, `to_dict()`, all tests). The frontend `EventSource` handler will read `event.prev_price` and get `undefined`.

**Impact:** Price flash animations and change calculations on the frontend will silently break.

**Fix:** Rename `previous_price` → `prev_price` in `models.py`, `cache.py`, all tests, and `CLAUDE.md`.

---

### 3. Timestamp format in SSE events is wrong — CRITICAL

**PLAN.md §6** requires ISO 8601:

```json
{"timestamp": "2026-04-10T12:00:00.500Z"}
```

`PriceUpdate.to_dict()` returns a raw Unix float (`1234567890.0`). The frontend will receive a number, not a parseable date string.

**Fix:** In `to_dict()` (or a dedicated `to_sse_dict()` method), convert `self.timestamp` to ISO format:

```python
from datetime import datetime, timezone
ts = datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
```

---

### 4. `massive_client.py` has top-level imports — HIGH

```python
# massive_client.py lines 8–9
from massive import RESTClient
from massive.rest.models import SnapshotMarketType
```

`factory.py` also imports `MassiveDataSource` eagerly at module load:

```python
# factory.py lines 10–11
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource
```

The design intent (and PLAN.md) was that `massive` is an optional dependency only needed when `MASSIVE_API_KEY` is set. The top-level imports mean the package is imported on every app startup regardless. Because `massive` is also listed as a hard dependency in `pyproject.toml`, this doesn't cause a runtime crash today — but it contradicts the spec and will cause issues in any deployment that doesn't install `massive` (e.g., students following a minimal setup, Docker builds that want a leaner image).

**Fix:** Move imports to inside the functions that use them (lazy imports):

```python
# factory.py
def create_market_data_source(price_cache):
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        from .massive_client import MassiveDataSource   # lazy
        return MassiveDataSource(...)
    else:
        from .simulator import SimulatorDataSource      # lazy
        return SimulatorDataSource(...)
```

Move `massive` to `[project.optional-dependencies]` in `pyproject.toml`.

---

### 5. SSE wire format deviates from spec — HIGH

`stream.py` sends all tickers in a single JSON object per tick:

```
data: {"AAPL": {"ticker": "AAPL", ...}, "GOOGL": {"ticker": "GOOGL", ...}}
```

PLAN.md §6 and `MARKET_INTERFACE.md` specify individual per-ticker events:

```
data: {"ticker": "AAPL", "price": 191.50, "prev_price": 191.32, ...}

data: {"ticker": "GOOGL", "price": 175.12, ...}
```

The frontend `EventSource.onmessage` handler will need to be written to match whichever format the backend produces. These are different enough that one handler cannot handle both. If the frontend is coded to the spec (individual events), it will silently ignore the batched format or need unwrapping.

This is worth resolving before the Frontend agent starts work so there is one unambiguous contract.

---

### 6. `stream.py` has 33% test coverage — HIGH

The SSE streaming endpoint is the core real-time feature and has essentially no test coverage. The untested code includes:

- The FastAPI route registration (`/api/stream/prices`)
- The `_generate_events` async generator — version-change detection, disconnect handling, event formatting

There are no tests that verify the wire format of SSE events, that the `retry` directive is sent, or that the generator stops on client disconnect. This is the highest-value gap in the test suite.

**Suggested tests:** Use `httpx` with `AsyncClient` and FastAPI's `TestClient` / async streaming to verify: (a) events are `text/event-stream`, (b) each event is valid JSON with the required fields, (c) version-based deduplication skips unchanged data.

---

### 7. Unknown ticker seed price is random, not $100 — MEDIUM

**PLAN.md §6:**

> When a ticker not in the default seed list is added, it starts with a seed price of **$100.00**.

**`simulator.py:151`:**

```python
self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
```

`seed_prices.py` correctly defines `DEFAULT_SEED_PRICE = 100.00`, but `simulator.py` does not import or use it — it uses an inline `random.uniform` instead.

**Impact:** Users adding custom tickers to the watchlist will see inconsistent, unpredictable starting prices rather than the specified $100.

**Fix:**

```python
from .seed_prices import DEFAULT_SEED_PRICE, SEED_PRICES, TICKER_PARAMS
# ...
self._prices[ticker] = SEED_PRICES.get(ticker, DEFAULT_SEED_PRICE)
```

---

### 8. `create_stream_router` mutates a module-level router — MINOR

```python
# stream.py
router = APIRouter(prefix="/api/stream", tags=["streaming"])

def create_stream_router(price_cache: PriceCache) -> APIRouter:
    @router.get("/prices")           # decorates the module-level router
    async def stream_prices(...):
        ...
    return router
```

Calling `create_stream_router()` more than once (e.g., in tests) will register duplicate routes on the same `router` object. The factory pattern should return a fresh router:

```python
def create_stream_router(price_cache: PriceCache) -> APIRouter:
    router = APIRouter(prefix="/api/stream", tags=["streaming"])
    @router.get("/prices")
    async def stream_prices(...):
        ...
    return router
```

---

### 9. TSLA correlation constant inconsistency — MINOR

`seed_prices.py` defines `TSLA_CORR = 0.3`. `MARKET_SIMULATOR.md` specifies `0.25` for TSLA ("loner" behaviour). Both values are reasonable, but the documentation and code are inconsistent. If the design intent was 0.25, update `seed_prices.py`. If 0.3 is intentional, update the docs.

---

## Summary

| # | Severity | Issue |
|---|----------|-------|
| 1 | Critical | `open_price` missing from `PriceUpdate` and `PriceCache` — breaks daily change % |
| 2 | Critical | Field named `previous_price`, spec requires `prev_price` — frontend contract broken |
| 3 | Critical | SSE timestamp is Unix float, spec requires ISO 8601 string |
| 4 | High | Top-level `massive` imports — breaks optional dependency design intent |
| 5 | High | SSE sends batched object, spec requires individual per-ticker events |
| 6 | High | `stream.py` has 33% coverage — SSE format/behaviour untested |
| 7 | Medium | Unknown ticker seeds at random price instead of $100 as specified |
| 8 | Minor | `create_stream_router` mutates a module-level router (duplicate route risk) |
| 9 | Minor | TSLA_CORR is 0.3 in code vs 0.25 in design docs |

**Issues 1, 2, 3, and 5** are a connected set — they all define the SSE/frontend contract. They should be resolved together before the Frontend agent starts consuming the stream, as they will otherwise require a coordinated frontend + backend change later.

**Issue 4 (lazy imports)** is a clean-up item that should be addressed to align with the documented design intent, even though it doesn't cause a runtime failure with the current `pyproject.toml`.

**Issue 6 (stream.py coverage)** is the most impactful gap to address in the test suite.
