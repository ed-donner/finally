"""Tests for the SSE streaming endpoint."""

from __future__ import annotations

import json

import pytest
from fastapi import APIRouter

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


class FakeClient:
    """Stand-in for starlette's Request.client (has a .host attribute)."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host


class FakeRequest:
    """Minimal stand-in for a starlette Request, just enough for _generate_events.

    `disconnect_after` controls how many `is_disconnected()` calls return False
    before the fake client "disconnects" (returns True).
    """

    def __init__(self, disconnect_after: int | None = None, host: str | None = "127.0.0.1") -> None:
        self._disconnect_after = disconnect_after
        self._checks = 0
        self.client = FakeClient(host) if host is not None else None

    async def is_disconnected(self) -> bool:
        self._checks += 1
        if self._disconnect_after is None:
            return False
        return self._checks > self._disconnect_after


async def _collect(agen, limit: int) -> list[str]:
    """Pull up to `limit` items out of an async generator."""
    items = []
    async for item in agen:
        items.append(item)
        if len(items) >= limit:
            break
    return items


class TestCreateStreamRouter:
    """Tests for the router factory."""

    def test_returns_api_router(self):
        cache = PriceCache()
        router = create_stream_router(cache)
        assert isinstance(router, APIRouter)

    def test_registers_prices_route(self):
        cache = PriceCache()
        router = create_stream_router(cache)
        paths = {route.path for route in router.routes}
        assert "/api/stream/prices" in paths

    def test_route_is_get_only(self):
        cache = PriceCache()
        router = create_stream_router(cache)
        route = next(r for r in router.routes if r.path == "/api/stream/prices")
        assert route.methods == {"GET"}


class TestGenerateEvents:
    """Tests for the underlying SSE event generator."""

    @pytest.mark.asyncio
    async def test_first_chunk_is_retry_directive(self):
        cache = PriceCache()
        request = FakeRequest(disconnect_after=0)
        chunks = await _collect(_generate_events(cache, request, interval=0.01), limit=1)
        assert chunks == ["retry: 1000\n\n"]

    @pytest.mark.asyncio
    async def test_stops_immediately_on_disconnect(self):
        cache = PriceCache()
        request = FakeRequest(disconnect_after=0)
        chunks = [c async for c in _generate_events(cache, request, interval=0.01)]
        # Only the retry directive is sent before the disconnect check trips.
        assert chunks == ["retry: 1000\n\n"]

    @pytest.mark.asyncio
    async def test_yields_price_data_for_populated_cache(self):
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        cache.update("GOOGL", 175.25)
        request = FakeRequest(disconnect_after=1)

        chunks = [c async for c in _generate_events(cache, request, interval=0.01)]

        assert chunks[0] == "retry: 1000\n\n"
        data_chunks = [c for c in chunks if c.startswith("data: ")]
        assert len(data_chunks) == 1

        payload = json.loads(data_chunks[0][len("data: ") : -2])
        assert set(payload.keys()) == {"AAPL", "GOOGL"}
        assert payload["AAPL"]["price"] == 190.50
        assert payload["AAPL"]["direction"] == "flat"

    @pytest.mark.asyncio
    async def test_empty_cache_sends_no_data_event(self):
        cache = PriceCache()
        request = FakeRequest(disconnect_after=1)

        chunks = [c async for c in _generate_events(cache, request, interval=0.01)]

        # No tickers in the cache -> only the retry directive, no `data:` frame.
        assert chunks == ["retry: 1000\n\n"]

    @pytest.mark.asyncio
    async def test_skips_resend_when_version_unchanged(self):
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        request = FakeRequest(disconnect_after=3)

        chunks = [c async for c in _generate_events(cache, request, interval=0.01)]

        # The cache never changes after the first update, so only one `data:`
        # frame should be emitted even though the loop runs multiple times.
        data_chunks = [c for c in chunks if c.startswith("data: ")]
        assert len(data_chunks) == 1

    @pytest.mark.asyncio
    async def test_sends_new_data_after_cache_update(self):
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        request = FakeRequest(disconnect_after=None)

        agen = _generate_events(cache, request, interval=0.01)
        # retry directive, then first price frame
        first_two = await _collect(agen, limit=2)
        assert first_two[1].startswith("data: ")

        # Trigger a new version and confirm the generator emits it next.
        cache.update("AAPL", 191.00)
        next_chunk = await agen.__anext__()
        assert next_chunk.startswith("data: ")
        payload = json.loads(next_chunk[len("data: ") : -2])
        assert payload["AAPL"]["price"] == 191.00
        await agen.aclose()

    @pytest.mark.asyncio
    async def test_handles_missing_client(self):
        """request.client can be None (e.g. behind certain test harnesses)."""
        cache = PriceCache()
        request = FakeRequest(disconnect_after=0, host=None)
        chunks = [c async for c in _generate_events(cache, request, interval=0.01)]
        assert chunks == ["retry: 1000\n\n"]
