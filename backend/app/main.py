"""FinAlly FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db.connection import get_db, init_db
from .market.cache import PriceCache
from .market.factory import create_market_data_source
from .market.interface import MarketDataSource
from .market.stream import create_stream_router
from .routers import chat, portfolio, watchlist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Load .env file if present (for local development)
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parents[3] / ".env")
except ImportError:
    pass

# Global singletons — created at module level so they can be injected
price_cache = PriceCache()
_market_source: MarketDataSource | None = None
_snapshot_task: asyncio.Task | None = None


def get_market_source() -> MarketDataSource | None:
    return _market_source


async def _snapshot_portfolio_loop() -> None:
    """Background task: snapshot portfolio value every 30 seconds."""
    while True:
        await asyncio.sleep(30)
        try:
            with get_db() as db:
                profile = db.execute(
                    "SELECT cash_balance FROM users_profile WHERE id = 'default'"
                ).fetchone()
                if not profile:
                    continue
                cash = profile["cash_balance"]

                positions = db.execute(
                    "SELECT ticker, quantity FROM positions WHERE user_id = 'default' AND quantity > 0"
                ).fetchall()
                pos_value = sum(
                    row["quantity"] * (price_cache.get_price(row["ticker"]) or 0)
                    for row in positions
                )
                total = cash + pos_value
                db.execute(
                    "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) VALUES (?, 'default', ?, ?)",
                    (str(uuid.uuid4()), total, datetime.now(timezone.utc).isoformat()),
                )
        except Exception as e:
            logger.error("Portfolio snapshot error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _market_source, _snapshot_task

    # Initialize database
    init_db()

    # Inject price cache into routers
    portfolio.set_price_cache(price_cache)
    watchlist.set_price_cache(price_cache)
    chat.set_price_cache(price_cache)

    # Get initial tickers from DB
    with get_db() as db:
        rows = db.execute(
            "SELECT ticker FROM watchlist WHERE user_id = 'default'"
        ).fetchall()
    initial_tickers = [r["ticker"] for r in rows]

    # Start market data source
    _market_source = create_market_data_source(price_cache)
    await _market_source.start(initial_tickers)
    logger.info("Market data source started with %d tickers", len(initial_tickers))

    # Start portfolio snapshot background task
    _snapshot_task = asyncio.create_task(_snapshot_portfolio_loop())

    yield

    # Shutdown
    if _snapshot_task:
        _snapshot_task.cancel()
        try:
            await _snapshot_task
        except asyncio.CancelledError:
            pass

    if _market_source:
        await _market_source.stop()
        logger.info("Market data source stopped")


app = FastAPI(
    title="FinAlly API",
    description="AI Trading Workstation backend",
    version="1.0.0",
    lifespan=lifespan,
)

# Health check
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "tickers_tracked": len(price_cache)}


# Register API routers
app.include_router(portfolio.router)
app.include_router(watchlist.router)
app.include_router(chat.router)

# SSE streaming — inject the module-level cache
stream_router = create_stream_router(price_cache)
app.include_router(stream_router)

# Static files — serve Next.js build
STATIC_DIR = Path(__file__).parent.parent.parent / "static"

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    logger.info("Serving static files from %s", STATIC_DIR)
else:
    @app.get("/")
    def root() -> dict[str, str]:
        return {"message": "FinAlly API is running. Frontend not built yet."}
