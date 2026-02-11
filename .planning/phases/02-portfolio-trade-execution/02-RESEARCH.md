# Phase 2: Portfolio & Trade Execution - Research

**Researched:** 2026-02-11
**Domain:** Portfolio management, trade execution, background snapshots (FastAPI + aiosqlite)
**Confidence:** HIGH

## Summary

Phase 2 builds the portfolio and trade execution service layer on top of the existing database foundation (Phase 1) and market data subsystem. The core challenge is straightforward CRUD with two subtleties: (1) trade execution must be atomic -- a buy must deduct cash AND create/update a position in a single transaction, and (2) portfolio snapshots require a periodic background task that runs every 30 seconds plus fires immediately after each trade.

The existing codebase provides all the building blocks: `aiosqlite` with WAL mode and `isolation_level=None` (autocommit mode, requiring explicit BEGIN/COMMIT for transactions), `PriceCache` with `get_price(ticker)` and `get_all()` for current market prices, and the Phase 1 database layer with all 6 tables already defined. The routes module (`backend/app/routes/`) exists as a directory but has no files yet -- this phase creates the first route files.

**Primary recommendation:** Create a `portfolio` service module that encapsulates all database operations with explicit transaction boundaries, a separate Pydantic models module for request/response schemas, and a thin route layer that delegates to the service. The portfolio snapshot background task should use `asyncio.create_task()` in a start/stop pattern (matching the market data source pattern), NOT FastAPI's `BackgroundTasks` which is request-scoped. The actual lifespan wiring happens in Phase 4 (App Assembly), so this phase exposes `start_snapshot_task` / `stop_snapshot_task` functions that Phase 4 will call.

## Standard Stack

### Core (already installed)

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| FastAPI | 0.128.7 | REST endpoints for portfolio/trade/history | Already in project |
| aiosqlite | 0.22.1 | Async SQLite operations with WAL mode | Already in project, Phase 1 foundation |
| Pydantic | 2.12.5 | Request/response models, validation | Bundled with FastAPI |
| uvicorn | 0.32.0+ | ASGI server | Already in project |

### Supporting (already installed)

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| httpx | 0.28.1 | Async test client for route testing | Testing route handlers |
| pytest-asyncio | 1.3.0 | Async test support with auto mode | All async tests |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Raw aiosqlite | SQLAlchemy async | Massive overkill for 6 tables with known schema; adds complexity |
| Manual BEGIN/COMMIT | Context manager wrapper | Nice-to-have but over-engineering for 2 transaction types |
| Separate snapshot service | APScheduler | External dependency for a simple `asyncio.sleep` loop |

**Installation:** No new dependencies needed. Everything is already in `pyproject.toml`.

## Architecture Patterns

### Recommended Project Structure

```
backend/app/
├── db/                      # [EXISTS] Database layer
│   ├── __init__.py
│   ├── connection.py
│   ├── schema.py
│   └── seed.py
├── market/                  # [EXISTS] Market data subsystem
│   ├── cache.py             # PriceCache - provides get_price(), get_all()
│   └── ...
├── portfolio/               # [NEW] Portfolio service layer
│   ├── __init__.py          # Re-exports: execute_trade, get_portfolio, etc.
│   ├── models.py            # Pydantic request/response schemas
│   ├── service.py           # Business logic + DB operations
│   └── snapshots.py         # Background snapshot task
└── routes/                  # [EXISTS as empty dir] API routes
    └── portfolio.py         # [NEW] FastAPI router for /api/portfolio/*
```

### Pattern 1: Service Layer with Dependency Injection

**What:** Separate business logic (service.py) from HTTP handling (routes/portfolio.py). The service receives `db` and `price_cache` as arguments -- it never imports globals.

**When to use:** Always for this project. Routes are thin dispatchers; services contain the logic.

**Example:**
```python
# backend/app/portfolio/service.py
from datetime import datetime, timezone
from uuid import uuid4
import aiosqlite
from app.market.cache import PriceCache

async def execute_trade(
    db: aiosqlite.Connection,
    price_cache: PriceCache,
    ticker: str,
    side: str,       # "buy" or "sell"
    quantity: float,
) -> dict:
    """Execute a market order. Returns trade details or raises ValueError."""
    current_price = price_cache.get_price(ticker)
    if current_price is None:
        raise ValueError(f"No price available for {ticker}")

    cost = round(current_price * quantity, 2)
    now = datetime.now(timezone.utc).isoformat()

    # Explicit transaction for atomicity
    await db.execute("BEGIN")
    try:
        if side == "buy":
            # Check cash
            cursor = await db.execute(
                "SELECT cash_balance FROM users_profile WHERE id = ?", ("default",)
            )
            row = await cursor.fetchone()
            if row["cash_balance"] < cost:
                await db.execute("ROLLBACK")
                raise ValueError(
                    f"Insufficient cash. Available: ${row['cash_balance']:.2f}, Required: ${cost:.2f}"
                )
            # Deduct cash
            await db.execute(
                "UPDATE users_profile SET cash_balance = cash_balance - ? WHERE id = ?",
                (cost, "default"),
            )
            # Upsert position
            await db.execute(
                """INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at)
                   VALUES (?, 'default', ?, ?, ?, ?)
                   ON CONFLICT(user_id, ticker) DO UPDATE SET
                     avg_cost = (positions.avg_cost * positions.quantity + ? * ?) / (positions.quantity + ?),
                     quantity = positions.quantity + ?,
                     updated_at = ?""",
                (str(uuid4()), ticker, quantity, current_price, now,
                 current_price, quantity, quantity, quantity, now),
            )
        elif side == "sell":
            # Check shares
            cursor = await db.execute(
                "SELECT quantity FROM positions WHERE user_id = ? AND ticker = ?",
                ("default", ticker),
            )
            row = await cursor.fetchone()
            if row is None or row["quantity"] < quantity:
                await db.execute("ROLLBACK")
                available = row["quantity"] if row else 0
                raise ValueError(
                    f"Insufficient shares. Available: {available}, Requested: {quantity}"
                )
            # Add cash
            await db.execute(
                "UPDATE users_profile SET cash_balance = cash_balance + ? WHERE id = ?",
                (cost, "default"),
            )
            new_qty = row["quantity"] - quantity
            if new_qty < 0.0001:  # Effectively zero
                await db.execute(
                    "DELETE FROM positions WHERE user_id = ? AND ticker = ?",
                    ("default", ticker),
                )
            else:
                await db.execute(
                    "UPDATE positions SET quantity = ?, updated_at = ? WHERE user_id = ? AND ticker = ?",
                    (new_qty, now, "default", ticker),
                )

        # Record trade (always)
        await db.execute(
            "INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid4()), "default", ticker, side, quantity, current_price, now),
        )

        await db.execute("COMMIT")
    except Exception:
        await db.execute("ROLLBACK")
        raise

    return {
        "ticker": ticker,
        "side": side,
        "quantity": quantity,
        "price": current_price,
        "total": cost,
    }
```

### Pattern 2: Explicit Transactions with isolation_level=None

**What:** The existing database connection uses `isolation_level=None` (autocommit mode). Each statement auto-commits unless wrapped in explicit `BEGIN`/`COMMIT`. For trade execution, we MUST wrap the read-check-write sequence in an explicit transaction.

**When to use:** Any operation that requires atomicity across multiple SQL statements (trade execution is the primary case). Single reads and single writes can rely on autocommit.

**Example:**
```python
# Atomic multi-step operation
await db.execute("BEGIN")
try:
    # Read current state
    cursor = await db.execute("SELECT cash_balance FROM users_profile WHERE id = ?", ("default",))
    row = await cursor.fetchone()
    # Validate
    if row["cash_balance"] < required_amount:
        await db.execute("ROLLBACK")
        raise ValueError("Insufficient cash")
    # Mutate
    await db.execute("UPDATE users_profile SET cash_balance = cash_balance - ? WHERE id = ?", (amount, "default"))
    await db.execute("COMMIT")
except Exception:
    await db.execute("ROLLBACK")
    raise
```

### Pattern 3: Route Factory for Dependency Injection

**What:** Follow the same factory pattern as `create_stream_router(price_cache)` in the existing market module. The route file exports a function that takes dependencies and returns a configured `APIRouter`.

**When to use:** All route modules in this project. Phase 4 (App Assembly) will call these factories and mount the returned routers.

**Example:**
```python
# backend/app/routes/portfolio.py
from fastapi import APIRouter, HTTPException

from app.market.cache import PriceCache
from app.portfolio.models import TradeRequest, TradeResponse, PortfolioResponse, PortfolioHistoryResponse
from app.portfolio import service

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


def create_portfolio_router(db, price_cache: PriceCache) -> APIRouter:

    @router.get("", response_model=PortfolioResponse)
    async def get_portfolio():
        return await service.get_portfolio(db, price_cache)

    @router.post("/trade", response_model=TradeResponse)
    async def trade(request: TradeRequest):
        try:
            result = await service.execute_trade(
                db, price_cache, request.ticker, request.side, request.quantity
            )
            return result
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.get("/history", response_model=PortfolioHistoryResponse)
    async def get_history():
        return await service.get_portfolio_history(db)

    return router
```

### Pattern 4: Background Snapshot Task

**What:** An `asyncio.create_task` based periodic loop that records portfolio value snapshots every 30 seconds. The task also exposes a `record_snapshot_now()` function called synchronously after each trade for immediate snapshots.

**When to use:** The snapshot task starts during app lifespan (Phase 4 wires this). This phase provides the functions; Phase 4 calls them.

**Example:**
```python
# backend/app/portfolio/snapshots.py
import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite
from app.market.cache import PriceCache

logger = logging.getLogger(__name__)
_snapshot_task: asyncio.Task | None = None


async def record_snapshot(db: aiosqlite.Connection, price_cache: PriceCache) -> None:
    """Record a single portfolio value snapshot."""
    cursor = await db.execute(
        "SELECT ticker, quantity FROM positions WHERE user_id = ?", ("default",)
    )
    positions = await cursor.fetchall()

    cursor = await db.execute(
        "SELECT cash_balance FROM users_profile WHERE id = ?", ("default",)
    )
    user = await cursor.fetchone()
    cash = user["cash_balance"]

    positions_value = 0.0
    for pos in positions:
        price = price_cache.get_price(pos["ticker"])
        if price is not None:
            positions_value += price * pos["quantity"]

    total_value = round(cash + positions_value, 2)
    now = datetime.now(timezone.utc).isoformat()

    await db.execute(
        "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) VALUES (?, ?, ?, ?)",
        (str(uuid4()), "default", total_value, now),
    )
    await db.commit()


async def _snapshot_loop(db: aiosqlite.Connection, price_cache: PriceCache, interval: float = 30.0) -> None:
    """Background loop that records snapshots periodically."""
    while True:
        try:
            await record_snapshot(db, price_cache)
        except Exception:
            logger.exception("Failed to record portfolio snapshot")
        await asyncio.sleep(interval)


def start_snapshot_task(db: aiosqlite.Connection, price_cache: PriceCache) -> asyncio.Task:
    """Start the periodic snapshot background task. Returns the task handle."""
    global _snapshot_task
    _snapshot_task = asyncio.create_task(_snapshot_loop(db, price_cache))
    return _snapshot_task


async def stop_snapshot_task() -> None:
    """Cancel the snapshot background task."""
    global _snapshot_task
    if _snapshot_task is not None:
        _snapshot_task.cancel()
        try:
            await _snapshot_task
        except asyncio.CancelledError:
            pass
        _snapshot_task = None
```

### Anti-Patterns to Avoid

- **Using FastAPI `BackgroundTasks` for periodic snapshots:** `BackgroundTasks` is request-scoped (runs after a single response). The snapshot task needs to run continuously on a timer independent of requests. Use `asyncio.create_task` instead.
- **Storing db connection as a global:** The market data module uses factory functions that receive dependencies. Follow the same pattern. Phase 4 will create the db connection in the lifespan and pass it to service factories.
- **Computing avg_cost in Python:** The weighted average cost calculation for position updates should happen in SQL using `ON CONFLICT ... DO UPDATE SET avg_cost = (old * old_qty + new * new_qty) / total_qty`. This is atomic and avoids race conditions.
- **Floating point equality for zero shares:** After a sell, don't check `new_qty == 0`. Use `new_qty < 0.0001` (epsilon) to handle floating point dust, then DELETE the position row.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Request validation | Custom if/else checks on request fields | Pydantic models with `Field(gt=0)` | FastAPI returns 422 automatically with clear error |
| Average cost calculation | Python-side read-modify-write | SQL `ON CONFLICT DO UPDATE SET` with arithmetic | Atomic, no race conditions |
| JSON serialization | Manual dict building | Pydantic `model_dump()` / `response_model` | FastAPI handles serialization automatically |
| Trade timestamp generation | Custom formatters | `datetime.now(timezone.utc).isoformat()` | Consistent with existing seed.py pattern |
| UUID generation | Custom ID schemes | `uuid4()` | Consistent with existing pattern in seed.py |

**Key insight:** The existing codebase already establishes patterns for IDs (uuid4), timestamps (ISO format), and database interaction (aiosqlite.Row with dict-like access). Follow these patterns exactly.

## Common Pitfalls

### Pitfall 1: Non-Atomic Trade Execution
**What goes wrong:** Reading cash balance, checking it's sufficient, then updating in separate auto-committed statements. Between the read and write, another concurrent operation could change the balance.
**Why it happens:** `isolation_level=None` means each statement auto-commits. A sequence of read-then-write without BEGIN/COMMIT is not atomic.
**How to avoid:** Wrap the entire trade execution (read balance + validate + update balance + upsert position + insert trade) in explicit `BEGIN`/`COMMIT`. Always `ROLLBACK` in the except handler.
**Warning signs:** Tests pass individually but fail under concurrent execution.

### Pitfall 2: Forgetting to ROLLBACK on Validation Failure
**What goes wrong:** After `BEGIN`, if a validation check fails (insufficient cash), the code raises an exception without calling `ROLLBACK`. The transaction stays open, potentially blocking subsequent operations.
**Why it happens:** The natural instinct is to just `raise ValueError()` after the check, forgetting that a transaction is open.
**How to avoid:** Always `ROLLBACK` before raising within a transaction, or use a try/except pattern where except always rolls back.
**Warning signs:** "database is locked" errors in subsequent operations.

### Pitfall 3: Average Cost Calculation Error on Position Update
**What goes wrong:** When buying more of an existing position, the average cost must be the weighted average: `(old_avg * old_qty + new_price * new_qty) / (old_qty + new_qty)`. Getting this formula wrong produces incorrect P&L calculations downstream.
**Why it happens:** Simple oversight, or forgetting to weight by quantity.
**How to avoid:** Use the SQL formula in `ON CONFLICT DO UPDATE SET`, and write a dedicated test that buys the same ticker twice at different prices and verifies the weighted average.
**Warning signs:** P&L numbers that don't add up.

### Pitfall 4: Sell Leaving Dust Shares
**What goes wrong:** After selling all shares, floating point arithmetic may leave `quantity = 0.0000000001` instead of exactly zero. The position still shows up in portfolio queries.
**Why it happens:** `float` arithmetic in SQLite (REAL type).
**How to avoid:** After computing `new_qty = old_qty - sell_qty`, check `if new_qty < 0.0001` and DELETE the position row instead of updating to near-zero.
**Warning signs:** "Ghost" positions with tiny quantities and meaningless P&L.

### Pitfall 5: Snapshot Task Not Awaiting Cancellation Properly
**What goes wrong:** Calling `task.cancel()` without awaiting the task means the cancellation may not complete before shutdown, potentially leaving database operations incomplete.
**Why it happens:** `cancel()` only requests cancellation; it doesn't wait for it.
**How to avoid:** After `task.cancel()`, `await task` inside a try/except for `CancelledError`.
**Warning signs:** Warnings about pending tasks during shutdown.

### Pitfall 6: Sell Average Cost Stays the Same
**What goes wrong:** Updating avg_cost when selling shares. The average cost should NOT change on a sell -- it only changes on buys.
**Why it happens:** Applying the same weighted-average formula to sells.
**How to avoid:** On sell, only update `quantity` and `updated_at`. Never touch `avg_cost`.
**Warning signs:** Average cost changing after sells, making P&L tracking unreliable.

## Code Examples

### Pydantic Models for Trade Request/Response

```python
# backend/app/portfolio/models.py
from pydantic import BaseModel, Field


class TradeRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)
    side: str = Field(..., pattern="^(buy|sell)$")
    quantity: float = Field(..., gt=0)


class TradeResponse(BaseModel):
    ticker: str
    side: str
    quantity: float
    price: float
    total: float


class PositionResponse(BaseModel):
    ticker: str
    quantity: float
    avg_cost: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_percent: float


class PortfolioResponse(BaseModel):
    cash_balance: float
    positions: list[PositionResponse]
    total_value: float


class SnapshotResponse(BaseModel):
    total_value: float
    recorded_at: str


class PortfolioHistoryResponse(BaseModel):
    snapshots: list[SnapshotResponse]
```

### Get Portfolio with Live Prices

```python
# In service.py
async def get_portfolio(
    db: aiosqlite.Connection, price_cache: PriceCache
) -> dict:
    """Build full portfolio view with live prices from PriceCache."""
    cursor = await db.execute(
        "SELECT cash_balance FROM users_profile WHERE id = ?", ("default",)
    )
    user = await cursor.fetchone()
    cash = user["cash_balance"]

    cursor = await db.execute(
        "SELECT ticker, quantity, avg_cost FROM positions WHERE user_id = ?",
        ("default",),
    )
    rows = await cursor.fetchall()

    positions = []
    positions_value = 0.0
    for row in rows:
        current_price = price_cache.get_price(row["ticker"]) or row["avg_cost"]
        market_value = round(current_price * row["quantity"], 2)
        cost_basis = round(row["avg_cost"] * row["quantity"], 2)
        unrealized_pnl = round(market_value - cost_basis, 2)
        pnl_pct = round((unrealized_pnl / cost_basis) * 100, 2) if cost_basis else 0.0

        positions.append({
            "ticker": row["ticker"],
            "quantity": row["quantity"],
            "avg_cost": row["avg_cost"],
            "current_price": current_price,
            "market_value": market_value,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_percent": pnl_pct,
        })
        positions_value += market_value

    return {
        "cash_balance": cash,
        "positions": positions,
        "total_value": round(cash + positions_value, 2),
    }
```

### Get Portfolio History

```python
async def get_portfolio_history(db: aiosqlite.Connection) -> dict:
    """Return portfolio value snapshots for charting."""
    cursor = await db.execute(
        "SELECT total_value, recorded_at FROM portfolio_snapshots "
        "WHERE user_id = ? ORDER BY recorded_at ASC",
        ("default",),
    )
    rows = await cursor.fetchall()
    return {
        "snapshots": [
            {"total_value": row["total_value"], "recorded_at": row["recorded_at"]}
            for row in rows
        ]
    }
```

### Testing Pattern: Service Layer with Test Database

```python
# backend/tests/portfolio/conftest.py
import pytest
from app.db import init_db
from app.market.cache import PriceCache


@pytest.fixture
async def db(tmp_path):
    """Isolated database per test."""
    conn = await init_db(str(tmp_path / "test.db"))
    yield conn
    await conn.close()


@pytest.fixture
def price_cache():
    """PriceCache pre-loaded with known prices."""
    cache = PriceCache()
    cache.update("AAPL", 150.00)
    cache.update("GOOGL", 175.00)
    cache.update("MSFT", 400.00)
    return cache
```

```python
# backend/tests/portfolio/test_service.py
from app.portfolio import service


async def test_buy_deducts_cash(db, price_cache):
    result = await service.execute_trade(db, price_cache, "AAPL", "buy", 10)
    assert result["price"] == 150.00
    assert result["total"] == 1500.00

    cursor = await db.execute(
        "SELECT cash_balance FROM users_profile WHERE id = ?", ("default",)
    )
    row = await cursor.fetchone()
    assert row["cash_balance"] == 8500.00  # 10000 - 1500


async def test_buy_creates_position(db, price_cache):
    await service.execute_trade(db, price_cache, "AAPL", "buy", 10)
    cursor = await db.execute(
        "SELECT quantity, avg_cost FROM positions WHERE user_id = ? AND ticker = ?",
        ("default", "AAPL"),
    )
    row = await cursor.fetchone()
    assert row["quantity"] == 10
    assert row["avg_cost"] == 150.00


async def test_insufficient_cash_rejected(db, price_cache):
    # AAPL at $150, buying 100 = $15000 > $10000
    with pytest.raises(ValueError, match="Insufficient cash"):
        await service.execute_trade(db, price_cache, "AAPL", "buy", 100)
```

### Testing Routes with httpx AsyncClient

```python
# backend/tests/routes/test_portfolio.py
import pytest
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI
from app.db import init_db
from app.market.cache import PriceCache
from app.routes.portfolio import create_portfolio_router


@pytest.fixture
async def app(tmp_path):
    db = await init_db(str(tmp_path / "test.db"))
    cache = PriceCache()
    cache.update("AAPL", 150.00)

    app = FastAPI()
    app.include_router(create_portfolio_router(db, cache))
    yield app
    await db.close()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_get_portfolio(client):
    response = await client.get("/api/portfolio")
    assert response.status_code == 200
    data = response.json()
    assert data["cash_balance"] == 10000.0
    assert data["positions"] == []
    assert data["total_value"] == 10000.0
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `@app.on_event("startup")` | `lifespan` async context manager | FastAPI 0.95+ (2023) | Phase 4 will use lifespan; this phase just provides startable/stoppable functions |
| `@pytest_asyncio.fixture` decorator | Standard `@pytest.fixture` with `asyncio_mode = "auto"` | pytest-asyncio 0.21+ | Existing conftest already uses this; follow same pattern |
| `isolation_level="DEFERRED"` (implicit txns) | `isolation_level=None` (explicit txns) | Project choice | Must use explicit BEGIN/COMMIT for multi-statement atomicity |
| Pydantic v1 `schema()` | Pydantic v2 `model_json_schema()` | Pydantic 2.0 (2023) | Use v2 patterns: `model_dump()`, `Field()`, `BaseModel` |

**Deprecated/outdated:**
- `@app.on_event("startup"/"shutdown")`: Replaced by lifespan. Not relevant to this phase but noted for Phase 4.
- `pytest.mark.asyncio` decorator on every test: Not needed with `asyncio_mode = "auto"` in pyproject.toml.

## Open Questions

1. **Snapshot task lifecycle ownership**
   - What we know: The snapshot task needs `db` and `price_cache` references. It runs continuously.
   - What's unclear: Whether this phase should wire it into a temporary test app, or just provide the functions and let Phase 4 wire them.
   - Recommendation: This phase provides `start_snapshot_task()` / `stop_snapshot_task()` / `record_snapshot()` functions and tests them directly (not through routes). Phase 4 wires them into the lifespan.

2. **Trade history endpoint**
   - What we know: PORT-06 requires trades to be recorded. The plan mentions "append-only log."
   - What's unclear: Whether a `GET /api/portfolio/trades` endpoint is needed in this phase, or just the recording.
   - Recommendation: Record every trade in the `trades` table. A dedicated history endpoint is not listed in the API spec for this phase, so just ensure trades are persisted. The data is queryable for Phase 5 (LLM context) and Phase 8 (frontend).

3. **Fallback price when PriceCache has no data**
   - What we know: In testing and during early startup, PriceCache may not have prices for all tickers yet.
   - What's unclear: Should `get_portfolio` fail, or use avg_cost as fallback?
   - Recommendation: Use `avg_cost` as fallback when PriceCache returns None. This makes portfolio always displayable. For trade execution, require a live price (reject if PriceCache has no price for the ticker).

## Sources

### Primary (HIGH confidence)
- Existing codebase inspection: `backend/app/db/connection.py` -- confirms `isolation_level=None` autocommit mode
- Existing codebase inspection: `backend/app/market/cache.py` -- confirms PriceCache API (`get_price`, `get_all`, `update`)
- Existing codebase inspection: `backend/app/market/stream.py` -- confirms factory pattern for route creation
- Existing codebase inspection: `backend/tests/db/conftest.py` -- confirms async fixture pattern with `tmp_path`
- [aiosqlite API Reference](https://aiosqlite.omnilib.dev/en/stable/api.html) -- transaction methods (commit, rollback, execute)
- [Python sqlite3 docs](https://docs.python.org/3/library/sqlite3.html) -- `isolation_level=None` behavior, explicit transaction control
- [FastAPI Lifespan Events](https://fastapi.tiangolo.com/advanced/events/) -- async context manager pattern

### Secondary (MEDIUM confidence)
- [FastAPI Error Handling](https://fastapi.tiangolo.com/tutorial/handling-errors/) -- HTTPException with status_code=400 for business logic errors
- [FastAPI Background Tasks discussion](https://dev.turmansolutions.ai/2025/09/27/understanding-fastapis-lifespan-events-proper-initialization-and-shutdown/) -- lifespan + create_task pattern for long-running tasks
- [aiosqlite GitHub](https://github.com/omnilib/aiosqlite) -- context manager behavior with isolation_level=None

### Tertiary (LOW confidence)
- None. All findings verified against codebase or official docs.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- everything is already installed and verified with `uv run python -c "import ..."`
- Architecture: HIGH -- follows existing patterns in market data module (factory functions, PriceCache injection)
- Transaction handling: HIGH -- verified `isolation_level=None` in existing code, confirmed behavior via Python docs
- Pitfalls: HIGH -- derived from known SQLite/aiosqlite behavior and existing code patterns
- Testing: HIGH -- existing test patterns in `tests/db/` and `tests/market/` provide clear template

**Research date:** 2026-02-11
**Valid until:** 2026-03-11 (stable stack, no expected breaking changes)
