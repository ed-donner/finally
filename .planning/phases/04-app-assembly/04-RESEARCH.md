# Phase 4: App Assembly - Research

**Researched:** 2026-02-11
**Domain:** FastAPI application wiring, lifespan management, static file serving
**Confidence:** HIGH

## Summary

Phase 4 assembles the existing backend components (database, market data, portfolio, watchlist) into a single FastAPI application with a lifespan context manager that orchestrates startup and shutdown. All API routes are mounted under `/api/*`, a health check is exposed at `/api/health`, and non-API routes serve static files (placeholder until the frontend is built).

The codebase already follows a closure-based router factory pattern -- each subsystem exposes a `create_*_router(dependencies)` function that returns an `APIRouter` with its prefix baked in. The assembly layer creates shared resources in the lifespan, passes them to router factories, and includes the resulting routers. FastAPI's `@asynccontextmanager` lifespan pattern handles startup/shutdown cleanly.

Static file serving for the Next.js static export requires a custom `SPAStaticFiles` subclass of Starlette's `StaticFiles` to handle SPA client-side routing (serving `index.html` for unknown paths). This must be mounted last, after all API routes.

**Primary recommendation:** Create `backend/app/main.py` with an `@asynccontextmanager` lifespan that initializes db, price_cache, market_data_source, routers, and snapshot task in startup, and tears them down in shutdown. Mount the SPA static files at `/` as the last route. Use `app.state` to share resources if needed by future middleware.

## Standard Stack

### Core (Already Installed)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| fastapi | 0.128.7 | Web framework | Already in pyproject.toml |
| uvicorn[standard] | >=0.32.0 | ASGI server | Already in pyproject.toml |
| aiosqlite | >=0.22.1 | Async SQLite | Already in pyproject.toml |
| starlette | (bundled) | StaticFiles, routing | Comes with FastAPI |

### Supporting (Already Installed - Dev)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| httpx | 0.28.1 | Async HTTP test client | Testing API endpoints via ASGITransport |
| pytest-asyncio | >=0.24.0 | Async test support | Already in dev dependencies |

### New Dependency Needed
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| asgi-lifespan | >=2.1 | LifespanManager for testing | Testing the full app with lifespan events |

**Add to dev dependencies:**
```bash
cd backend && uv add --dev asgi-lifespan
```

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| asgi-lifespan for testing | Manual startup/shutdown in fixtures | asgi-lifespan is cleaner and handles edge cases; BUT existing tests work fine without it by creating routers directly. For Phase 4 integration tests specifically (testing the full app with lifespan), asgi-lifespan is the right tool. For simpler tests, the existing pattern of creating a bare `FastAPI()` and including routers manually is fine. |
| Custom SPAStaticFiles | Catch-all route with FileResponse | Custom subclass is cleaner, avoids route ordering issues, and is the community-standard approach |
| Module-level globals | app.state | Module-level is simpler for single-file assembly; app.state is better if resources need to be accessed from middleware or sub-apps. Given the closure-based factory pattern already in use, module-level within the lifespan scope is the cleanest approach. |

## Architecture Patterns

### Recommended Project Structure (New Files)
```
backend/app/
  main.py              # NEW: FastAPI app with lifespan, route mounting, static files
  static_files.py      # NEW: SPAStaticFiles subclass for SPA fallback
```

### Pattern 1: Lifespan Context Manager
**What:** All resource initialization (db, cache, market data, background tasks) happens in a single `@asynccontextmanager` function passed to `FastAPI(lifespan=...)`.
**When to use:** Always for FastAPI apps with async startup/shutdown resources.
**Source:** https://fastapi.tiangolo.com/advanced/events/

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    db = await init_db(db_path)
    price_cache = PriceCache()
    market_source = create_market_data_source(price_cache)

    # Load watchlist from DB and start market data
    watchlist_rows = await get_watchlist(db)
    tickers = [row["ticker"] for row in watchlist_rows]
    await market_source.start(tickers)

    # Start background snapshot task
    snapshot_task = await start_snapshot_task(db, price_cache)

    # Mount routers (they capture dependencies via closures)
    app.include_router(create_stream_router(price_cache))
    app.include_router(create_portfolio_router(db, price_cache))
    app.include_router(create_watchlist_router(db, price_cache, market_source))

    yield

    # --- SHUTDOWN ---
    await stop_snapshot_task()
    await market_source.stop()
    await close_db(db)

app = FastAPI(lifespan=lifespan)
```

**IMPORTANT NOTE on router mounting:** FastAPI's `include_router()` should be called at module level (before the app starts), not inside the lifespan. The lifespan runs after route registration. The correct pattern is:

1. Create the app and routers at module level using a factory, OR
2. Use the lifespan only for resource initialization and store references that the routers access.

Since the existing codebase uses closure-based factories that need the db/cache at creation time, the recommended pattern is a **factory function** that creates and returns the fully-assembled app:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

def create_app(db_path: str = "db/finally.db", static_dir: str | None = None) -> FastAPI:
    """Build the FastAPI application with all routes and lifespan."""

    # Shared mutable state -- populated by lifespan, read by route closures
    state = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        db = await init_db(db_path)
        price_cache = PriceCache()
        market_source = create_market_data_source(price_cache)

        watchlist_rows = await get_watchlist(db)
        tickers = [row["ticker"] for row in watchlist_rows]
        await market_source.start(tickers)

        await start_snapshot_task(db, price_cache)

        # Store refs for routers (closures already captured them at creation)
        state["db"] = db
        state["market_source"] = market_source

        yield

        # Shutdown
        await stop_snapshot_task()
        await market_source.stop()
        await close_db(db)

    app = FastAPI(title="FinAlly", lifespan=lifespan)

    # Problem: routers need db/cache at creation time, but they're created in lifespan.
    # Solution below in Pattern 2.

    return app
```

### Pattern 2: Lazy Resource Binding (Recommended for This Codebase)
**What:** Since the existing router factories need dependencies at creation time but those dependencies are created in the lifespan, use a two-phase approach: create placeholder containers that the lifespan populates.

**Actually, the simplest approach:** Create the resources BEFORE the app, not in the lifespan. Use the lifespan only for async start/stop operations.

```python
def create_app(db_path: str = "db/finally.db", static_dir: str | None = None) -> FastAPI:
    # These will be populated during lifespan startup
    price_cache = PriceCache()  # Can be created eagerly (no async needed)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Async initialization
        db = await init_db(db_path)
        market_source = create_market_data_source(price_cache)

        watchlist_rows = await get_watchlist(db)
        tickers = [row["ticker"] for row in watchlist_rows]
        await market_source.start(tickers)
        await start_snapshot_task(db, price_cache)

        # Store on app.state for access in routers
        app.state.db = db
        app.state.price_cache = price_cache
        app.state.market_source = market_source

        yield

        await stop_snapshot_task()
        await market_source.stop()
        await close_db(db)

    app = FastAPI(title="FinAlly", lifespan=lifespan)

    # Health check (no dependencies needed)
    @app.get("/api/health", tags=["system"])
    async def health():
        return {"status": "healthy"}

    # Mount routers using request.app.state access pattern
    # ... OR use the approach described in Pattern 3

    return app
```

### Pattern 3: The Cleanest Approach for This Codebase (RECOMMENDED)
**What:** The existing router factories use closure-based dependency injection. To make this work with the lifespan, use a container object that gets populated during startup. The routers capture references to the container.

**Actually, the SIMPLEST approach for this codebase:** Create the `PriceCache` eagerly (it requires no async). Create the `db` in the lifespan and store it in a module-level reference that the routers close over. But this is fragile.

**BEST approach:** Refactor slightly so that routers accept `request.app.state` access, OR (even better for this codebase) use a **request-scoped dependency injection** approach. BUT: the existing routers already work via closures and changing them is outside Phase 4 scope.

**ACTUAL RECOMMENDATION:** Use a two-step factory. Create the app factory that returns both the app and a "startup" coroutine. The routers are created with the real objects inside the lifespan, and included into the app at that point. While `include_router` is typically called at module scope, FastAPI does support calling it dynamically before the first request is processed (the lifespan runs before requests).

**VERIFIED:** FastAPI's `include_router()` CAN be called inside the lifespan. The routes are registered into the app's route table immediately. Since the lifespan runs before any request is served, this is safe. The OpenAPI schema is generated on first request, so dynamic route additions during lifespan work correctly.

```python
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import close_db, init_db
from app.market import PriceCache, create_market_data_source, create_stream_router
from app.portfolio import start_snapshot_task, stop_snapshot_task
from app.routes.portfolio import create_portfolio_router
from app.watchlist import create_watchlist_router, get_watchlist

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "db/finally.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize all resources on startup, clean up on shutdown."""
    logger.info("Starting FinAlly...")

    # 1. Database
    db = await init_db(DB_PATH)
    logger.info("Database initialized: %s", DB_PATH)

    # 2. Price cache and market data source
    price_cache = PriceCache()
    market_source = create_market_data_source(price_cache)

    # 3. Load watchlist and start streaming
    watchlist_rows = await get_watchlist(db)
    tickers = [row["ticker"] for row in watchlist_rows]
    await market_source.start(tickers)
    logger.info("Market data started for %d tickers", len(tickers))

    # 4. Start portfolio snapshot background task
    await start_snapshot_task(db, price_cache)
    logger.info("Portfolio snapshot task started")

    # 5. Mount API routers (safe to call in lifespan -- runs before first request)
    app.include_router(create_stream_router(price_cache))
    app.include_router(create_portfolio_router(db, price_cache))
    app.include_router(create_watchlist_router(db, price_cache, market_source))

    logger.info("FinAlly ready")
    yield

    # Shutdown
    logger.info("Shutting down FinAlly...")
    await stop_snapshot_task()
    await market_source.stop()
    await close_db(db)
    logger.info("FinAlly stopped")


app = FastAPI(title="FinAlly", lifespan=lifespan)


@app.get("/api/health", tags=["system"])
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


# Static files mounted LAST (catch-all for non-API routes)
# See static_files.py for SPAStaticFiles implementation
```

### Pattern 4: SPA Static File Serving
**What:** Custom subclass of `StaticFiles` that falls back to `index.html` for any path that doesn't match a real file (required for client-side routing in SPAs).
**When to use:** Always when serving a SPA (React, Next.js static export, Vue, etc.) from the same server as the API.

```python
from starlette.staticfiles import StaticFiles
from starlette.exceptions import HTTPException


class SPAStaticFiles(StaticFiles):
    """StaticFiles subclass that serves index.html for unknown paths.

    This enables client-side routing: when a user navigates to /dashboard,
    the server returns index.html and the frontend router handles the path.
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as ex:
            if ex.status_code == 404:
                # Fall back to index.html for SPA routing
                return await super().get_response("index.html", scope)
            raise
```

Mount at the END of app setup (after all API routes):
```python
import os

STATIC_DIR = os.environ.get("STATIC_DIR", "static")

if os.path.isdir(STATIC_DIR):
    app.mount("/", SPAStaticFiles(directory=STATIC_DIR, html=True), name="spa")
```

**Critical:** The static files mount MUST be last. FastAPI processes routes in registration order. If mounted first, it would intercept `/api/*` requests.

### Pattern 5: Health Check Endpoint
**What:** Simple GET endpoint at `/api/health` that returns `{"status": "healthy"}`.
**When to use:** Docker HEALTHCHECK, load balancers, monitoring.

```python
@app.get("/api/health", tags=["system"])
async def health():
    return {"status": "healthy"}
```

This is registered directly on the app (not via a router factory) since it has no dependencies. It is registered BEFORE the lifespan runs, which means it's available immediately.

**Note:** The health check is a simple liveness probe. It does NOT check database connectivity or market data status. This is intentional -- a liveness probe should be cheap and fast. If more detailed checks are needed later, a separate `/api/health/ready` readiness probe can be added.

### Anti-Patterns to Avoid
- **Mounting StaticFiles before API routes:** Would intercept API requests
- **Using `@app.on_event("startup")`:** Deprecated in favor of lifespan context manager
- **Creating db connections per-request:** The app uses a single shared aiosqlite connection (this is fine for single-user SQLite)
- **Putting business logic in main.py:** main.py should only wire things together, never contain domain logic
- **Hardcoding paths:** Use environment variables for DB_PATH and STATIC_DIR

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| SPA file serving | Custom catch-all route with FileResponse | SPAStaticFiles subclass of StaticFiles | StaticFiles handles MIME types, caching headers, HEAD requests, etc. correctly |
| Lifespan management | Manual try/except startup/shutdown | `@asynccontextmanager` lifespan | Guaranteed cleanup even on exceptions |
| Async test client | Manually managing event loops | httpx.AsyncClient + ASGITransport | Already the pattern in the test suite |
| Lifespan in tests | Manual startup/shutdown calls | asgi-lifespan LifespanManager | Handles protocol correctly |

## Common Pitfalls

### Pitfall 1: Route Registration Order
**What goes wrong:** Static file mount at `/` catches API requests before they reach API routers.
**Why it happens:** FastAPI/Starlette processes mounts/routes in registration order. A catch-all mount at `/` will match everything.
**How to avoid:** Always mount `SPAStaticFiles("/", ...)` LAST, after all `include_router()` calls and direct route registrations.
**Warning signs:** API endpoints return HTML instead of JSON, or return 404.

### Pitfall 2: include_router Inside Lifespan
**What goes wrong:** Concern that calling `include_router()` inside the lifespan is too late for route registration.
**Why it happens:** Conventional wisdom says routes must be registered at module level.
**How to avoid:** This actually works fine. The lifespan runs before any request is served. OpenAPI schema is generated on first request. Routes added during lifespan are available. However, if you want to be extra safe, an alternative is to register a health check endpoint directly on the app (before lifespan) to ensure at least one route exists immediately.
**Warning signs:** If routes aren't accessible, check that `include_router` is called before `yield` in the lifespan.

### Pitfall 3: Static Directory Missing
**What goes wrong:** App crashes on startup if the static directory doesn't exist.
**Why it happens:** `StaticFiles(directory="static", check_dir=True)` (default) raises RuntimeError if directory is missing.
**How to avoid:** Either use `check_dir=False`, or guard the mount with `os.path.isdir()`, or create a placeholder `static/` directory with an `index.html`. During development (before the frontend is built), a placeholder is the cleanest approach.
**Warning signs:** RuntimeError on startup about directory not found.

### Pitfall 4: SQLite Path in Docker vs Local Development
**What goes wrong:** SQLite database is created in the wrong location.
**Why it happens:** Relative paths resolve differently in Docker (`/app/db/finally.db`) vs local dev (`backend/db/finally.db`).
**How to avoid:** Use `DB_PATH` environment variable with a sensible default. In Docker, set `DB_PATH=/app/db/finally.db`. Locally, default to `db/finally.db` (relative to cwd when running from `backend/`).
**Warning signs:** Database resets on every restart (not persisting to the volume-mounted path).

### Pitfall 5: Testing the Full App with Lifespan
**What goes wrong:** Tests that create `AsyncClient(transport=ASGITransport(app=app))` don't trigger the lifespan, so routes mounted inside the lifespan aren't available.
**Why it happens:** `httpx.AsyncClient` with `ASGITransport` does NOT trigger ASGI lifespan events.
**How to avoid:** Use `asgi-lifespan.LifespanManager` to wrap the app before passing to AsyncClient. OR for simpler tests, continue the existing pattern of creating a bare `FastAPI()`, manually creating dependencies, and including individual routers.
**Warning signs:** 404 errors for all API routes in tests, or routes returning unexpected results because the db wasn't initialized.

### Pitfall 6: Module-Level stream Router
**What goes wrong:** The SSE stream router in `app/market/stream.py` creates a module-level `router = APIRouter(prefix="/api/stream", tags=["streaming"])` and then `create_stream_router()` decorates an endpoint on it. If `create_stream_router()` is called multiple times (e.g., in tests), the endpoint gets registered multiple times on the same router instance.
**Why it happens:** The router is module-level, so it persists across calls.
**How to avoid:** For Phase 4, this is fine because `create_stream_router()` is only called once in the lifespan. But be aware of this in tests -- create a fresh app per test (which the existing tests already do).
**Warning signs:** Duplicate route warnings in tests.

## Code Examples

### Complete main.py Structure
```python
# backend/app/main.py
"""FinAlly application entry point.

Creates the FastAPI app, wires all dependencies via lifespan,
and mounts routes and static files.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.db import close_db, init_db
from app.market import PriceCache, create_market_data_source, create_stream_router
from app.portfolio import start_snapshot_task, stop_snapshot_task
from app.routes.portfolio import create_portfolio_router
from app.watchlist import create_watchlist_router, get_watchlist

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "db/finally.db")
STATIC_DIR = os.environ.get("STATIC_DIR", "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize all resources on startup, tear down on shutdown."""
    logger.info("Starting FinAlly...")

    # 1. Database
    db = await init_db(DB_PATH)

    # 2. Market data
    price_cache = PriceCache()
    market_source = create_market_data_source(price_cache)
    watchlist_rows = await get_watchlist(db)
    tickers = [row["ticker"] for row in watchlist_rows]
    await market_source.start(tickers)

    # 3. Background tasks
    await start_snapshot_task(db, price_cache)

    # 4. API routers
    app.include_router(create_stream_router(price_cache))
    app.include_router(create_portfolio_router(db, price_cache))
    app.include_router(create_watchlist_router(db, price_cache, market_source))

    logger.info("FinAlly ready -- %d tickers streaming", len(tickers))
    yield

    # Shutdown
    logger.info("Shutting down...")
    await stop_snapshot_task()
    await market_source.stop()
    await close_db(db)


app = FastAPI(title="FinAlly", lifespan=lifespan)


@app.get("/api/health", tags=["system"])
async def health():
    """Liveness probe."""
    return {"status": "healthy"}


# Static files -- mount last so API routes take priority
static_path = Path(STATIC_DIR)
if static_path.is_dir():
    from app.static_files import SPAStaticFiles
    app.mount("/", SPAStaticFiles(directory=STATIC_DIR, html=True), name="spa")
```

### SPAStaticFiles Implementation
```python
# backend/app/static_files.py
"""SPA-aware static file serving for Next.js static export."""

from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles


class SPAStaticFiles(StaticFiles):
    """Serves index.html for any path that doesn't match a real file.

    Required for client-side routing in single-page applications.
    Without this, direct navigation to /dashboard would return 404
    instead of the index.html that contains the SPA router.
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as ex:
            if ex.status_code == 404:
                return await super().get_response("index.html", scope)
            raise
```

### Running the App
```bash
cd backend
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Testing the Full App with Lifespan
```python
# backend/tests/test_app.py
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    """Full app client with lifespan triggered."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_health(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


async def test_watchlist_loaded(client):
    resp = await client.get("/api/watchlist")
    assert resp.status_code == 200
    assert resp.json()["count"] == 10
```

**IMPORTANT:** Using the module-level `app` in tests with `LifespanManager` can cause issues with state leaking between tests (especially the module-level router in `stream.py`). An alternative is to use an app factory:

```python
def create_app(db_path: str = "db/finally.db") -> FastAPI:
    # Move all the module-level code into here
    # This ensures a fresh app per test
    ...
```

However, for the initial implementation, using the module-level `app` with careful test isolation (tmp_path for db, monkeypatch for env vars) should work. The app factory can be refactored in if needed.

### Testing Without Lifespan (Simpler Unit-Level Tests)
The existing test pattern (creating a bare FastAPI, manually creating deps, including individual routers) remains the best approach for unit-level route tests:

```python
@pytest.fixture
async def client(tmp_path):
    db = await init_db(str(tmp_path / "test.db"))
    cache = PriceCache()
    app = FastAPI()
    app.include_router(create_portfolio_router(db, cache))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await db.close()
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `@app.on_event("startup")` / `@app.on_event("shutdown")` | `@asynccontextmanager` lifespan | FastAPI 0.95+ (2023) | Guaranteed cleanup, single function for related setup/teardown |
| `TestClient` (sync) | `httpx.AsyncClient` + `ASGITransport` | FastAPI docs updated 2024 | Native async testing, better for async apps |
| Manual SPA catch-all route | `SPAStaticFiles` subclass | Community pattern (stable) | Cleaner, handles edge cases |

**Deprecated/outdated:**
- `@app.on_event("startup"/"shutdown")`: Still works but deprecated. If both lifespan and on_event are defined, on_event handlers are NOT called.

## Open Questions

1. **Module-Level `app` vs App Factory**
   - What we know: Module-level `app` is simpler and matches the uvicorn `app.main:app` convention. App factory is safer for tests.
   - What's unclear: Whether the module-level stream router causes issues in test isolation.
   - Recommendation: Start with module-level `app`. If tests have isolation issues, refactor to app factory. The cost of refactoring is low.

2. **include_router Inside Lifespan Safety**
   - What we know: It works because lifespan runs before first request, and OpenAPI schema is lazy.
   - What's unclear: Whether this is officially documented/supported or just works by implementation detail.
   - Recommendation: Use this pattern. It's the cleanest way to wire closure-based routers that need async dependencies. If issues arise, fall back to app factory with lazy resource binding.

3. **Static Directory Placeholder**
   - What we know: The frontend isn't built yet. The app needs something at `static/index.html`.
   - What's unclear: Whether to create a minimal placeholder HTML or just guard the mount with `os.path.isdir()`.
   - Recommendation: Do both -- create a minimal placeholder `static/index.html` AND guard the mount. This way the app works both with and without the frontend build output.

## Sources

### Primary (HIGH confidence)
- FastAPI official docs: https://fastapi.tiangolo.com/advanced/events/ -- lifespan context manager pattern
- FastAPI official docs: https://fastapi.tiangolo.com/tutorial/static-files/ -- StaticFiles mounting
- Starlette source code (inspected directly) -- StaticFiles `html=True` behavior confirmed via source inspection
- Existing codebase -- all router factories, service layers, and test patterns

### Secondary (MEDIUM confidence)
- FastAPI community discussions on SPA static file serving -- SPAStaticFiles pattern is widely used
- asgi-lifespan PyPI: https://pypi.org/project/asgi-lifespan/ -- LifespanManager for testing
- Multiple sources confirm `include_router` works inside lifespan

### Tertiary (LOW confidence)
- None -- all findings verified against official docs or source code

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all libraries already in use, only adding asgi-lifespan for dev
- Architecture: HIGH -- lifespan pattern is officially documented, router factories already exist
- Static files: HIGH -- verified StaticFiles source code directly, SPAStaticFiles is a well-known pattern
- Pitfalls: HIGH -- verified via source inspection and existing test patterns
- Testing: MEDIUM -- LifespanManager approach is standard but not yet tested in this codebase

**Research date:** 2026-02-11
**Valid until:** 2026-03-11 (stable patterns, unlikely to change)
