"""FinAlly FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import db
from .chat_api import router as chat_router
from .market import PriceCache, create_market_data_source, create_stream_router
from .portfolio.routes import router as portfolio_router
from .snapshot_task import run_snapshot_loop
from .state import AppState
from .watchlist_api import router as watchlist_router

logger = logging.getLogger(__name__)

STATIC_DIR_ENV = "FINALLY_STATIC_DIR"
DEFAULT_STATIC_DIR = "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start market data + snapshot loop, then shut them down cleanly."""
    state: AppState = app.state.finally_state
    initial_tickers = db.watchlist.list_tickers()
    await state.market_source.start(initial_tickers)
    logger.info("market data source started with %d tickers", len(initial_tickers))

    stop_event = asyncio.Event()
    snapshot_task = asyncio.create_task(run_snapshot_loop(state.price_cache, stop_event))

    try:
        yield
    finally:
        stop_event.set()
        snapshot_task.cancel()
        try:
            await snapshot_task
        except (asyncio.CancelledError, Exception):
            pass
        await state.market_source.stop()
        logger.info("market data source stopped")


def _make_health_router() -> APIRouter:
    router = APIRouter(tags=["system"])

    @router.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return router


def create_app() -> FastAPI:
    # Load .env from project root (one level up from backend/) if present.
    # Harmless if missing; Docker passes env vars via --env-file.
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

    app = FastAPI(title="FinAlly", lifespan=lifespan)

    # Allow the Next.js dev server (port 3000) to call the API cross-origin.
    # In production the frontend is served from the same origin as the API,
    # so CORS is a no-op.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Initialize DB and shared market state up front so all routes —
    # including the SSE stream — can be registered before the static
    # catch-all mount below. The market source is started/stopped in lifespan.
    db.ensure_initialized()
    cache = PriceCache()
    source = create_market_data_source(cache)
    app.state.finally_state = AppState(price_cache=cache, market_source=source)

    # API routes (registered before static mount so /api/* always wins)
    app.include_router(_make_health_router())
    app.include_router(portfolio_router)
    app.include_router(watchlist_router)
    app.include_router(chat_router)
    app.include_router(create_stream_router(cache))

    # Static frontend (Next.js export). Optional — directory may not exist in dev.
    static_dir = os.environ.get(STATIC_DIR_ENV, DEFAULT_STATIC_DIR)
    static_path = Path(static_dir)
    if static_path.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=str(static_path), html=True),
            name="static",
        )
        logger.info("serving static frontend from %s", static_path)
    else:
        logger.info("no static frontend at %s — API only", static_path)

    return app


app = create_app()
