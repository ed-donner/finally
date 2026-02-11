# Phase 3: Watchlist API - Research

**Researched:** 2026-02-11
**Domain:** FastAPI REST endpoints, SQLite CRUD, market data integration
**Confidence:** HIGH

## Summary

The Watchlist API is a straightforward CRUD layer connecting the existing SQLite `watchlist` table to three REST endpoints, with the critical addition of syncing watchlist changes to the live `MarketDataSource` so added tickers start streaming and removed tickers stop. All infrastructure is already in place: the database schema, the `MarketDataSource` interface with `add_ticker()`/`remove_ticker()`, and the `PriceCache` for enriching responses with live prices.

The main architectural decision is how to structure the service layer and wire dependencies (database connection, PriceCache, MarketDataSource) into FastAPI routes. The existing codebase uses a factory pattern for routers (see `create_stream_router` in `app/market/stream.py`), and the watchlist router should follow the same pattern for consistency.

**Primary recommendation:** Create a watchlist service module with pure async functions that take the db connection as a parameter, a router factory that injects dependencies via closure (matching the existing `create_stream_router` pattern), and Pydantic v2 models for request/response serialization.

## Standard Stack

### Core (already installed)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| FastAPI | 0.128.7 | REST endpoints, routing, validation | Already in use |
| Pydantic | 2.12.5 | Request/response models, validation | Ships with FastAPI |
| aiosqlite | 0.22.1 | Async SQLite access | Already in use for DB layer |
| uvicorn | 0.40.0 | ASGI server | Already in use |

### Supporting (for testing, already installed)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pytest | 8.3+ | Test runner | All tests |
| pytest-asyncio | 0.24+ | Async test support | Async endpoint/service tests |
| httpx | 0.28.1 | Async HTTP client for testing | API endpoint tests |

### New Dependency Needed
| Library | Version | Purpose | Notes |
|---------|---------|---------|-------|
| httpx | 0.28.1 | Test client for FastAPI | Already installed as transitive dep of FastAPI, but should be added explicitly to `[project.optional-dependencies] dev` in pyproject.toml |

**Installation:**
```bash
cd backend
uv add --dev httpx
```

## Architecture Patterns

### Recommended Project Structure

```
backend/app/
├── market/              # Existing - market data subsystem
│   ├── __init__.py
│   ├── cache.py         # PriceCache
│   ├── interface.py     # MarketDataSource ABC
│   ├── simulator.py     # SimulatorDataSource
│   ├── massive_client.py # MassiveDataSource
│   ├── stream.py        # SSE router factory
│   └── ...
├── db/                  # Existing - database layer
│   ├── __init__.py
│   ├── connection.py    # init_db, close_db
│   ├── schema.py        # CREATE TABLE statements
│   └── seed.py          # Default data
├── watchlist/           # NEW - watchlist feature
│   ├── __init__.py      # Public API exports
│   ├── models.py        # Pydantic request/response models
│   ├── service.py       # Database operations (pure async functions)
│   └── router.py        # FastAPI router factory
└── __init__.py
```

### Pattern 1: Router Factory with Closure-Based DI

**What:** Create routers via factory functions that capture dependencies in closures. This matches the existing `create_stream_router()` pattern.

**When to use:** When you have shared state (db connection, PriceCache, MarketDataSource) that multiple endpoints need.

**Example:**
```python
# Source: existing pattern from app/market/stream.py
from fastapi import APIRouter

def create_watchlist_router(
    db: aiosqlite.Connection,
    price_cache: PriceCache,
    market_data_source: MarketDataSource,
) -> APIRouter:
    router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

    @router.get("")
    async def get_watchlist() -> WatchlistResponse:
        tickers = await watchlist_service.get_watchlist(db)
        # Enrich with prices from cache
        ...
        return WatchlistResponse(items=items)

    @router.post("", status_code=201)
    async def add_ticker(request: AddTickerRequest) -> WatchlistItem:
        item = await watchlist_service.add_ticker(db, request.ticker)
        await market_data_source.add_ticker(request.ticker)
        return item

    @router.delete("/{ticker}", status_code=200)
    async def remove_ticker(ticker: str) -> dict:
        await watchlist_service.remove_ticker(db, ticker)
        await market_data_source.remove_ticker(ticker)
        return {"removed": ticker}

    return router
```

### Pattern 2: Service Layer with Pure Async Functions

**What:** Keep database logic in a separate service module with functions that accept the db connection as a parameter. No classes needed -- plain functions are simpler and easier to test.

**When to use:** For all database CRUD operations.

**Example:**
```python
# watchlist/service.py
async def get_watchlist(db: aiosqlite.Connection, user_id: str = "default") -> list[dict]:
    cursor = await db.execute(
        "SELECT id, ticker, added_at FROM watchlist WHERE user_id = ? ORDER BY added_at",
        (user_id,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]

async def add_ticker(db: aiosqlite.Connection, ticker: str, user_id: str = "default") -> dict:
    ticker = ticker.upper().strip()
    row_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
        (row_id, user_id, ticker, now),
    )
    await db.commit()
    return {"id": row_id, "ticker": ticker, "added_at": now}

async def remove_ticker(db: aiosqlite.Connection, ticker: str, user_id: str = "default") -> bool:
    ticker = ticker.upper().strip()
    cursor = await db.execute(
        "DELETE FROM watchlist WHERE user_id = ? AND ticker = ?",
        (user_id, ticker),
    )
    await db.commit()
    return cursor.rowcount > 0
```

### Pattern 3: Pydantic v2 Models for Request/Response

**What:** Use Pydantic BaseModel for request bodies and response serialization. FastAPI 0.128+ supports return type annotations (preferred over `response_model`).

**Example:**
```python
# watchlist/models.py
from pydantic import BaseModel, Field

class AddTickerRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10, examples=["AAPL"])

class WatchlistItem(BaseModel):
    ticker: str
    added_at: str
    price: float | None = None
    change: float | None = None
    change_percent: float | None = None
    direction: str | None = None

class WatchlistResponse(BaseModel):
    items: list[WatchlistItem]
    count: int
```

### Anti-Patterns to Avoid

- **Global mutable state for DI:** Do NOT use module-level globals for the db connection or price cache. Use the factory pattern with closures instead. This matches the existing codebase and is testable.
- **Mixing DB logic into route handlers:** Keep route handlers thin. They orchestrate calls to the service layer and market data source but contain no SQL themselves.
- **Returning raw dicts from endpoints:** Use Pydantic models for automatic validation, documentation, and type safety.
- **Forgetting to normalize ticker case:** Always `.upper().strip()` ticker strings at the service boundary. The database has UNIQUE(user_id, ticker), so "aapl" vs "AAPL" matters.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Request validation | Manual if/else checks | Pydantic `Field(min_length=1)` | Automatic 422 errors with clear messages |
| Duplicate detection | Try/except in route | SQLite UNIQUE constraint + IntegrityError catch | Database is the source of truth |
| JSON serialization | Manual dict building | Pydantic `.model_dump()` / return type annotations | FastAPI handles serialization automatically |
| Ticker normalization | Ad-hoc per endpoint | Single `.upper().strip()` in service layer | Consistent, one place to change |

## Common Pitfalls

### Pitfall 1: IntegrityError on Duplicate Ticker
**What goes wrong:** POST /api/watchlist with a ticker already in the watchlist raises `sqlite3.IntegrityError` due to the UNIQUE(user_id, ticker) constraint, crashing the endpoint with a 500 error.
**Why it happens:** The schema enforces uniqueness, which is correct, but the route must handle this gracefully.
**How to avoid:** Catch `sqlite3.IntegrityError` in the service or router and raise `HTTPException(status_code=409, detail="Ticker already in watchlist")`.
**Warning signs:** Uncaught exceptions in POST endpoint tests.

### Pitfall 2: Forgetting to Sync with MarketDataSource
**What goes wrong:** Ticker is added to the database but `market_data_source.add_ticker()` is never called, so the ticker appears in the watchlist but has no live price data.
**Why it happens:** The database and market data source are separate systems that must be kept in sync.
**How to avoid:** The router layer must call both the service (database) AND the market data source for add/remove operations. The order should be: database first (to validate/persist), then market data source.
**Warning signs:** Watchlist items with `null` prices after add.

### Pitfall 3: Remove Non-Existent Ticker Returns 200
**What goes wrong:** DELETE /api/watchlist/FAKE returns 200 even though the ticker was never in the watchlist, confusing the frontend.
**Why it happens:** `DELETE FROM watchlist WHERE ticker = ?` succeeds silently when no rows match.
**How to avoid:** Check `cursor.rowcount` after the DELETE. If 0, raise `HTTPException(status_code=404, detail="Ticker not in watchlist")`.
**Warning signs:** Delete tests pass without proper 404 assertion.

### Pitfall 4: Case Sensitivity in Ticker Matching
**What goes wrong:** User adds "aapl" but the database has "AAPL" from seed data. The UNIQUE constraint sees them as different, so both exist. Or DELETE fails to find "AAPL" when passed "aapl".
**Why it happens:** SQLite text comparison is case-sensitive by default.
**How to avoid:** Always normalize to uppercase at the service boundary: `ticker = ticker.upper().strip()`.
**Warning signs:** Duplicate tickers in watchlist with different casing.

### Pitfall 5: Not Awaiting MarketDataSource Methods
**What goes wrong:** `add_ticker()` and `remove_ticker()` on `MarketDataSource` are async methods. Forgetting `await` means they never execute, and the market data source is out of sync.
**Why it happens:** Easy to miss since the db operations are also async and properly awaited.
**How to avoid:** Both `MarketDataSource.add_ticker()` and `MarketDataSource.remove_ticker()` must be awaited.
**Warning signs:** Linter warning about unawaited coroutine; ticker added to DB but no price data.

## Code Examples

### Enriching Watchlist with Live Prices
```python
# Combine DB watchlist with PriceCache data
async def get_watchlist_with_prices(
    db: aiosqlite.Connection,
    price_cache: PriceCache,
    user_id: str = "default",
) -> list[dict]:
    cursor = await db.execute(
        "SELECT ticker, added_at FROM watchlist WHERE user_id = ? ORDER BY added_at",
        (user_id,),
    )
    rows = await cursor.fetchall()

    items = []
    for row in rows:
        ticker = row["ticker"]
        price_update = price_cache.get(ticker)
        item = {
            "ticker": ticker,
            "added_at": row["added_at"],
        }
        if price_update:
            item.update({
                "price": price_update.price,
                "change": price_update.change,
                "change_percent": price_update.change_percent,
                "direction": price_update.direction,
            })
        items.append(item)
    return items
```

### Handling Duplicate Ticker Insert
```python
import sqlite3
from fastapi import HTTPException

async def add_ticker(db: aiosqlite.Connection, ticker: str, user_id: str = "default") -> dict:
    ticker = ticker.upper().strip()
    row_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        await db.execute(
            "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
            (row_id, user_id, ticker, now),
        )
        await db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"{ticker} is already in the watchlist")
    return {"id": row_id, "ticker": ticker, "added_at": now}
```

### Testing with httpx AsyncClient
```python
# Source: FastAPI official docs - async testing
import pytest
from httpx import ASGITransport, AsyncClient

# The test fixture creates a FastAPI app with mocked dependencies
@pytest.fixture
async def client(tmp_path):
    from app.db import init_db
    from app.market.cache import PriceCache
    from app.watchlist.router import create_watchlist_router

    db = await init_db(str(tmp_path / "test.db"))
    cache = PriceCache()

    # Use a mock MarketDataSource for tests
    mock_source = MockMarketDataSource()

    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(create_watchlist_router(db, cache, mock_source))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac

    await db.close()


async def test_get_watchlist(client):
    response = await client.get("/api/watchlist")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert data["count"] == 10  # Seed data


async def test_add_ticker(client):
    response = await client.post("/api/watchlist", json={"ticker": "PYPL"})
    assert response.status_code == 201
    data = response.json()
    assert data["ticker"] == "PYPL"


async def test_add_duplicate_ticker(client):
    response = await client.post("/api/watchlist", json={"ticker": "AAPL"})
    assert response.status_code == 409


async def test_remove_ticker(client):
    response = await client.delete("/api/watchlist/AAPL")
    assert response.status_code == 200
    assert response.json()["removed"] == "AAPL"


async def test_remove_nonexistent_ticker(client):
    response = await client.delete("/api/watchlist/FAKE")
    assert response.status_code == 404
```

### Mock MarketDataSource for Testing
```python
from app.market.interface import MarketDataSource

class MockMarketDataSource(MarketDataSource):
    """Minimal mock for testing watchlist operations."""

    def __init__(self):
        self._tickers: list[str] = []
        self.added: list[str] = []    # Track calls for assertions
        self.removed: list[str] = []  # Track calls for assertions

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(tickers)

    async def stop(self) -> None:
        self._tickers = []

    async def add_ticker(self, ticker: str) -> None:
        self._tickers.append(ticker)
        self.added.append(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        self.removed.append(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `response_model=X` parameter | Return type annotation `-> X` | FastAPI 0.100+ | Cleaner syntax, better IDE support |
| `@pytest.mark.asyncio` per test | `asyncio_mode = "auto"` in config | pytest-asyncio 0.21+ | No decorators needed on async tests |
| TestClient (sync) | httpx.AsyncClient with ASGITransport | httpx 0.23+ | Required for testing async endpoints properly |
| Pydantic v1 `.dict()` | Pydantic v2 `.model_dump()` | Pydantic 2.0 | New API, old method deprecated |

**Already configured in this project:**
- `asyncio_mode = "auto"` is set in pyproject.toml -- no `@pytest.mark.asyncio` needed on individual test functions
- Pydantic v2 (2.12.5) is installed
- FastAPI 0.128.7 supports return type annotations

## Open Questions

1. **Should the watchlist router factory also receive the main FastAPI app, or be returned and included by the caller?**
   - What we know: The existing `create_stream_router` returns an `APIRouter` that the caller includes via `app.include_router()`.
   - Recommendation: Follow the same pattern. Return `APIRouter` from the factory.

2. **Where will the FastAPI app be assembled?**
   - What we know: There is no `main.py` or app entrypoint yet. The watchlist API needs to be wired into a FastAPI app with the db, cache, and market data source.
   - Recommendation: This phase should create the router factory but may need to create a minimal `app/main.py` for integration testing. The full app assembly may be a separate phase. For now, tests can create a minimal FastAPI app inline.

3. **Should ticker validation check against a known list of valid symbols?**
   - What we know: The PLAN.md doesn't specify ticker validation beyond CRUD. The simulator will accept any ticker string and generate random prices for it.
   - Recommendation: Accept any non-empty uppercase string for now. The simulator's `DEFAULT_PARAMS` provides reasonable defaults for unknown tickers. Validation against a symbol list could be a future enhancement.

## Sources

### Primary (HIGH confidence)
- Existing codebase: `backend/app/market/interface.py` - MarketDataSource ABC with add_ticker/remove_ticker
- Existing codebase: `backend/app/market/cache.py` - PriceCache with get/get_all/remove
- Existing codebase: `backend/app/market/stream.py` - Router factory pattern (create_stream_router)
- Existing codebase: `backend/app/db/schema.py` - Watchlist table schema
- Existing codebase: `backend/app/db/seed.py` - Default watchlist tickers
- Existing codebase: `backend/tests/` - Test patterns (asyncio_mode=auto, fixtures)
- FastAPI official docs: https://fastapi.tiangolo.com/tutorial/response-model/ - Return type annotations
- FastAPI official docs: https://fastapi.tiangolo.com/advanced/async-tests/ - httpx AsyncClient testing
- FastAPI official docs: https://fastapi.tiangolo.com/advanced/testing-dependencies/ - Dependency overrides

### Secondary (MEDIUM confidence)
- FastAPI official docs: https://fastapi.tiangolo.com/tutorial/handling-errors/ - HTTPException patterns

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - All libraries already installed and verified in codebase
- Architecture: HIGH - Following existing patterns from the market data subsystem
- Pitfalls: HIGH - Based on direct schema analysis and interface review
- Testing: HIGH - Verified httpx 0.28.1 installed, pytest-asyncio configured with auto mode

**Research date:** 2026-02-11
**Valid until:** 2026-03-11 (stable domain, low churn)
