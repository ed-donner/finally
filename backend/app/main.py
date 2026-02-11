"""FinAlly main application — FastAPI with lifespan management."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.chat.routes import create_chat_router
from app.db import init_db
from app.market import PriceCache, create_market_data_source, create_stream_router
from app.portfolio import PortfolioService
from app.portfolio.routes import create_portfolio_router
from app.watchlist import WatchlistService
from app.watchlist.routes import create_watchlist_router

logger = logging.getLogger(__name__)

SNAPSHOT_INTERVAL = 30  # seconds


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle: DB, market data, snapshot task."""
    # Initialize database
    db = init_db()
    logger.info("Database initialized")

    # Create shared price cache
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # Get initial watchlist tickers
    watchlist_service = WatchlistService(db, price_cache)
    tickers = watchlist_service.get_tickers()

    # Start market data source
    market_source = create_market_data_source(price_cache)
    await market_source.start(tickers)
    app.state.market_source = market_source
    logger.info("Market data source started with %d tickers", len(tickers))

    # Mount API routers
    app.include_router(create_stream_router(price_cache))
    app.include_router(create_portfolio_router(price_cache))
    app.include_router(create_watchlist_router(price_cache))
    app.include_router(create_chat_router(price_cache))

    # Mount static frontend files (after API routers so /api/* routes take priority)
    static_dir = Path(__file__).resolve().parents[1] / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    # Start portfolio snapshot background task
    snapshot_task = asyncio.create_task(_snapshot_loop(db, price_cache))

    yield

    # Shutdown
    snapshot_task.cancel()
    try:
        await snapshot_task
    except asyncio.CancelledError:
        pass
    await market_source.stop()
    logger.info("Application shutdown complete")


async def _snapshot_loop(db, price_cache: PriceCache) -> None:
    """Record portfolio snapshots at regular intervals."""
    await asyncio.sleep(5)  # Wait for initial prices
    service = PortfolioService(db, price_cache)
    while True:
        try:
            service.record_snapshot()
        except Exception:
            logger.exception("Failed to record portfolio snapshot")
        await asyncio.sleep(SNAPSHOT_INTERVAL)


def create_app() -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(title="FinAlly", version="0.1.0", lifespan=lifespan)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
