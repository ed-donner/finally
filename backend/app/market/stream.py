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

PUSH_INTERVAL = 0.5  # seconds between change checks
HEARTBEAT_INTERVAL = 15.0  # comment line when idle; keeps proxies from closing the stream


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create a router exposing GET /api/stream/prices. A fresh router per call."""
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """Full map of tracked prices whenever the cache changes (checked every 500 ms).

        retry: 1000
        id: <cache version>
        data: {"AAPL": {"ticker": "AAPL", "price": 190.5, ...}, ...}
        """
        return StreamingResponse(
            generate_price_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


async def generate_price_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = PUSH_INTERVAL,
    heartbeat: float = HEARTBEAT_INTERVAL,
) -> AsyncGenerator[str, None]:
    """Yield SSE frames until the client disconnects."""
    yield "retry: 1000\n\n"
    last_version = -1
    last_sent = time.monotonic()
    client = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client)
    try:
        while not await request.is_disconnected():
            version = price_cache.version
            if version != last_version:
                last_version = version
                payload = {t: u.to_dict() for t, u in price_cache.get_all().items()}
                yield f"id: {version}\ndata: {json.dumps(payload)}\n\n"
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= heartbeat:
                yield ": keepalive\n\n"
                last_sent = time.monotonic()
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("SSE client disconnected: %s", client)
