"""SSE streaming endpoint for live price updates."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # Disable nginx buffering if proxied
}


def create_stream_router(
    price_cache: PriceCache,
    interval: float = 0.5,
    keepalive_seconds: float = 15.0,
) -> APIRouter:
    """Create the SSE streaming router with a reference to the price cache.

    A fresh APIRouter is built on every call, so multiple app instances (e.g.
    in tests) never register duplicate routes on a shared router.
    """
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices", response_class=StreamingResponse)
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint for live price updates.

        Whenever the cache changes, sends the full map of tracked tickers:

            data: {"AAPL": {"ticker": "AAPL", "price": 190.50, ...}, ...}

        An empty cache is sent as `data: {}`. Includes a retry directive so
        the browser auto-reconnects (EventSource built-in behavior).
        """
        return StreamingResponse(
            _generate_events(price_cache, request, interval, keepalive_seconds),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    return router


def format_prices_event(price_cache: PriceCache) -> str:
    """Serialize the whole cache as one SSE `data:` frame (may be `{}`)."""
    data = {ticker: update.to_dict() for ticker, update in price_cache.get_all().items()}
    return f"data: {json.dumps(data)}\n\n"


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
    keepalive_seconds: float = 15.0,
) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE-formatted price events.

    Emits a frame whenever price_cache.version changes (checked every
    `interval` seconds), and an SSE comment as keep-alive when idle for
    `keepalive_seconds`. Stops when the client disconnects.
    """
    # Tell the client to retry after 1 second if the connection drops
    yield "retry: 1000\n\n"

    last_version = -1  # guarantees an initial frame, even for an empty cache
    last_sent = time.monotonic()
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                yield format_prices_event(price_cache)
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= keepalive_seconds:
                yield ": keep-alive\n\n"
                last_sent = time.monotonic()

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
        raise
