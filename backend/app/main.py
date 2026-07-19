"""FastAPI application entry point for FinAlly.

Wires the database, the shared market-data cache + source, the SSE stream,
the REST API, and (in production) the static frontend export — all on one port.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import init_db, repo
from app.market import PriceCache, create_market_data_source, create_stream_router
from app.runtime import set_runtime
from app.services.portfolio import build_portfolio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("finally")

SNAPSHOT_INTERVAL_SECONDS = 30


async def _snapshot_loop() -> None:
    """Periodically record the portfolio's total value for the P&L chart."""
    while True:
        try:
            await asyncio.sleep(SNAPSHOT_INTERVAL_SECONDS)
            repo.insert_snapshot(build_portfolio()["total_value"])
        except asyncio.CancelledError:
            break
        except Exception:  # pragma: no cover - keep the loop alive
            logger.exception("Snapshot loop iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Database (lazy schema + seed)
    init_db(settings.db_path)

    # Market source writes into the cache created in create_app()
    cache: PriceCache = app.state.price_cache
    source = create_market_data_source(cache)
    set_runtime(cache, source)

    tickers = repo.get_watchlist()
    await source.start(tickers)
    logger.info("Market source started with %d tickers", len(tickers))

    # Seed an initial snapshot + start the periodic snapshotter
    repo.insert_snapshot(build_portfolio()["total_value"])
    snapshot_task = asyncio.create_task(_snapshot_loop())

    try:
        yield
    finally:
        snapshot_task.cancel()
        try:
            await snapshot_task
        except asyncio.CancelledError:
            pass
        await source.stop()
        logger.info("Market source stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="FinAlly", version="1.0.0", lifespan=lifespan)

    # Single shared cache; created now so routers can bind to it before the
    # catch-all static mount is registered.
    cache = PriceCache()
    app.state.price_cache = cache

    from app.api import chat, health, portfolio, watchlist

    app.include_router(create_stream_router(cache))
    app.include_router(health.router)
    app.include_router(portfolio.router)
    app.include_router(watchlist.router)
    app.include_router(chat.router)

    # Serve the built frontend (Next.js static export) in production — must be
    # registered LAST so it doesn't shadow the /api routes above.
    static_dir = settings.static_dir
    if os.path.isdir(static_dir):
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
        logger.info("Serving static frontend from %s", static_dir)
    else:
        logger.info("No static dir at '%s' — API-only mode (dev)", static_dir)

    return app


app = create_app()
