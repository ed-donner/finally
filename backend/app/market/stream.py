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


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create the SSE streaming router with a reference to the price cache.

    A fresh ``APIRouter`` is built per call so the factory has no shared module
    state — calling it twice yields two independent routers rather than
    registering ``/prices`` twice on one global router.
    """
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint for live price updates.

        Streams all tracked ticker prices every ~500ms. The client connects
        with EventSource and receives events in the format:

            data: {"AAPL": {"ticker": "AAPL", "price": 190.50, ...}, ...}

        Includes a retry directive so the browser auto-reconnects on
        disconnection (EventSource built-in behavior).
        """
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering if proxied
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
    heartbeat: float = 15.0,
) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE-formatted price events.

    Sends all prices every `interval` seconds when they change. If no update
    has been sent for `heartbeat` seconds (prices flat, source idle, etc.), a
    comment ping is emitted to keep the connection alive through proxies and
    browsers that drop idle event-streams. Stops when the client disconnects
    (detected via request.is_disconnected()).
    """
    # Tell the client to retry after 1 second if the connection drops
    yield "retry: 1000\n\n"

    last_version = -1
    last_send = time.monotonic()
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            # Check for client disconnect
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            # Atomic (version, prices) read so they can't be torn apart by a
            # concurrent cache update.
            current_version, prices = price_cache.snapshot()
            if current_version != last_version:
                last_version = current_version
                if prices:
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    payload = json.dumps(data)
                    yield f"data: {payload}\n\n"
                    last_send = time.monotonic()
            elif time.monotonic() - last_send >= heartbeat:
                # Keep-alive comment — ignored by EventSource, but keeps the
                # TCP/proxy connection from being reaped while prices are flat.
                yield ": keep-alive\n\n"
                last_send = time.monotonic()

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
        raise
    finally:
        # Runs on every exit path (normal disconnect, cancellation, error) so
        # connection bookkeeping is always balanced against the connect log.
        logger.info("SSE stream closed for: %s", client_ip)
