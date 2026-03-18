"""FinAlly FastAPI application."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import init_db, get_positions, get_user_profile, list_watchlist
from app.market import PriceCache, create_market_data_source, create_stream_router

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Create the shared price cache at module level so the SSE router can bind to it.
# The lifespan reuses this same instance and stores it on app.state for API routers.
_price_cache = PriceCache()


async def _snapshot_loop(app: FastAPI, interval: float = 30.0):
    """Background task that records portfolio snapshots every `interval` seconds."""
    from app.db import insert_portfolio_snapshot

    while True:
        await asyncio.sleep(interval)
        try:
            cache = app.state.price_cache
            profile = await get_user_profile()
            if not profile:
                continue
            positions = await get_positions()
            total_value = profile["cash_balance"]
            for pos in positions:
                price_update = cache.get(pos["ticker"])
                price = price_update.price if price_update else pos["avg_cost"]
                total_value += price * pos["quantity"]
            await insert_portfolio_snapshot(round(total_value, 2))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Snapshot task error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Use the module-level cache, create market data source
    source = create_market_data_source(_price_cache)

    app.state.price_cache = _price_cache
    app.state.market_source = source

    # Get watchlist tickers and start market data
    watchlist = await list_watchlist()
    tickers = [w["ticker"] for w in watchlist]
    await source.start(tickers)
    logger.info("Market data source started with %d tickers", len(tickers))

    # Start background snapshot task
    snapshot_task = asyncio.create_task(_snapshot_loop(app))

    yield

    # Shutdown
    snapshot_task.cancel()
    try:
        await snapshot_task
    except asyncio.CancelledError:
        pass
    await source.stop()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    """Build the complete FastAPI app."""
    application = FastAPI(title="FinAlly", version="0.1.0", lifespan=lifespan)

    from app.api.health import router as health_router
    from app.api.portfolio import router as portfolio_router
    from app.api.watchlist import router as watchlist_router
    from app.api.chat import router as chat_router

    application.include_router(health_router)
    application.include_router(portfolio_router)
    application.include_router(watchlist_router)
    application.include_router(chat_router)

    # SSE streaming router
    stream_router = create_stream_router(_price_cache)
    application.include_router(stream_router)

    # Serve static frontend files (if directory exists)
    if STATIC_DIR.is_dir():
        application.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return application


app = create_app()
