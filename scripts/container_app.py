"""Container runtime ASGI entrypoint.

Prefers importing the configured backend app module. If unavailable, it boots a
minimal fallback app that serves static assets, an API health endpoint, and the
market SSE router.
"""

from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

LOGGER = logging.getLogger("finally.container")
STATIC_DIR = Path("/app/static")


def _load_target_app() -> Any | None:
    app_module = os.getenv("APP_MODULE", "app.main:app").strip()
    if ":" not in app_module:
        LOGGER.warning("APP_MODULE '%s' is invalid. Expected module:attribute", app_module)
        return None

    module_name, attribute_name = app_module.split(":", 1)

    try:
        module = importlib.import_module(module_name)
        target = getattr(module, attribute_name)
    except Exception as exc:  # pragma: no cover - fallback path
        LOGGER.warning("Unable to load APP_MODULE '%s': %s", app_module, exc)
        return None

    if not callable(target):
        LOGGER.warning("APP_MODULE '%s' did not resolve to a callable ASGI app", app_module)
        return None

    LOGGER.info("Using backend app from APP_MODULE=%s", app_module)
    return target


def _build_fallback_app() -> FastAPI:
    app = FastAPI(title="FinAlly Container Fallback")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": "fallback"}

    cache = None
    source = None

    try:
        from app.market import PriceCache, create_market_data_source, create_stream_router
        from app.market.seed_prices import SEED_PRICES

        cache = PriceCache()
        source = create_market_data_source(cache)
        app.include_router(create_stream_router(cache))

        @app.on_event("startup")
        async def startup_market() -> None:
            await source.start(list(SEED_PRICES.keys()))

        @app.on_event("shutdown")
        async def shutdown_market() -> None:
            await source.stop()

    except Exception as exc:  # pragma: no cover - optional fallback feature
        LOGGER.warning("Market stream fallback disabled: %s", exc)

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


app = _load_target_app() or _build_fallback_app()
