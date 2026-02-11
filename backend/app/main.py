"""FinAlly application entry point.

Wires together all backend subsystems via a lifespan context manager:
database, market data, portfolio snapshots, and API routes.
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import close_db, init_db
from app.market import PriceCache, create_market_data_source, create_stream_router
from app.portfolio import start_snapshot_task, stop_snapshot_task
from app.routes.portfolio import create_portfolio_router
from app.llm import create_chat_router
from app.watchlist import create_watchlist_router, get_watchlist

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "db/finally.db")
STATIC_DIR = os.environ.get("STATIC_DIR", "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle: startup and shutdown of all subsystems."""
    logger.info("Starting FinAlly...")

    # Database
    db = await init_db(DB_PATH)

    # Market data
    price_cache = PriceCache()
    market_source = create_market_data_source(price_cache)

    # Load watchlist and start streaming
    rows = await get_watchlist(db)
    tickers = [r["ticker"] for r in rows]
    await market_source.start(tickers)

    # Portfolio snapshots
    await start_snapshot_task(db, price_cache)

    # Mount routers
    app.include_router(create_stream_router(price_cache))
    app.include_router(create_portfolio_router(db, price_cache))
    app.include_router(create_watchlist_router(db, price_cache, market_source))
    app.include_router(create_chat_router(db, price_cache, market_source))

    # Static files last so API routes take priority
    if os.path.isdir(STATIC_DIR):
        from app.static_files import SPAStaticFiles

        app.mount("/", SPAStaticFiles(directory=STATIC_DIR, html=True), name="spa")

    logger.info("FinAlly ready with %d tickers", len(tickers))
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
