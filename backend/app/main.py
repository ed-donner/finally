"""FinAlly FastAPI application: lifespan, router wiring and static frontend serving."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db
from app.config import get_settings  # loads .env; env vars are read at call time
from app.market import PriceCache, create_market_data_source, create_stream_router
from app.routes import chat, health, portfolio, watchlist
from app.services.portfolio import record_portfolio_snapshot
from app.services.watchlist import tracked_tickers

logger = logging.getLogger(__name__)


async def _snapshot_loop(price_cache: PriceCache, interval: float) -> None:
    """Record a portfolio value snapshot every ``interval`` seconds."""
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(record_portfolio_snapshot, price_cache)
        except Exception:
            logger.exception("Portfolio snapshot failed")


def _register_static(app: FastAPI, static_dir: Path) -> None:
    """Serve the Next.js static export with SPA index.html fallback (after all API routes)."""
    if not static_dir.is_dir():
        logger.info("Static dir %s not found; serving API only", static_dir)
        return
    root = static_dir.resolve()
    index = root / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise StarletteHTTPException(status_code=404, detail="Not Found")
        candidate = (root / full_path).resolve()
        if candidate.is_relative_to(root):
            if candidate.is_file():
                return FileResponse(candidate)
            # Next.js export: /foo -> foo.html or foo/index.html
            for alt in (candidate.with_name(candidate.name + ".html"), candidate / "index.html"):
                if full_path and alt.is_file() and alt.resolve().is_relative_to(root):
                    return FileResponse(alt)
        if index.is_file():
            return FileResponse(index)
        raise StarletteHTTPException(status_code=404, detail="Not Found")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    await asyncio.to_thread(db.init_db)

    price_cache: PriceCache = app.state.price_cache
    initial_prices = {p["ticker"]: float(p["avg_cost"]) for p in db.get_positions()}
    source = create_market_data_source(price_cache, initial_prices=initial_prices)
    await source.start(sorted(tracked_tickers()))

    app.state.market_source = source

    snapshot_task = asyncio.create_task(_snapshot_loop(price_cache, settings.snapshot_interval))
    logger.info("FinAlly started: tracking %d tickers", len(source.get_tickers()))
    try:
        yield
    finally:
        snapshot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await snapshot_task
        await source.stop()
        logger.info("FinAlly stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="FinAlly", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Contract: request validation failures are 400 with a readable string detail.
        parts = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
            parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
        return JSONResponse(status_code=400, content={"detail": "; ".join(parts) or "Invalid request"})

    # One cache per app; the SSE router binds it up front, the lifespan's source fills it.
    app.state.price_cache = PriceCache()
    app.include_router(create_stream_router(app.state.price_cache))
    app.include_router(health.router)
    app.include_router(portfolio.router)
    app.include_router(watchlist.router)
    app.include_router(chat.router)

    _register_static(app, get_settings().static_dir)
    return app


app = create_app()
