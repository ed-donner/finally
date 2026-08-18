"""Tests for the SSE streaming endpoint (stream.py)."""

import json
from unittest.mock import MagicMock

import pytest

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


def _make_request(disconnected_after: int = 1):
    """Mock Request whose is_disconnected() returns False `disconnected_after`
    times, then True — simulates a client that stays connected for N checks.
    """
    request = MagicMock()
    request.client.host = "127.0.0.1"
    calls = {"n": 0}

    async def is_disconnected():
        calls["n"] += 1
        return calls["n"] > disconnected_after

    request.is_disconnected = is_disconnected
    return request


@pytest.mark.asyncio
class TestGenerateEvents:
    async def test_first_yield_is_retry_directive(self):
        cache = PriceCache()
        request = _make_request()
        gen = _generate_events(cache, request, interval=0.01)
        first = await gen.__anext__()
        assert first == "retry: 1000\n\n"

    async def test_sends_seeded_ticker_data(self):
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        request = _make_request(disconnected_after=1)

        events = [event async for event in _generate_events(cache, request, interval=0.01)]

        data_events = [e for e in events if e.startswith("data: ")]
        assert len(data_events) == 1
        payload = json.loads(data_events[0][len("data: ") :].strip())
        assert "AAPL" in payload
        assert payload["AAPL"]["price"] == 190.50

    async def test_no_data_event_when_cache_empty(self):
        cache = PriceCache()
        request = _make_request(disconnected_after=1)

        events = [event async for event in _generate_events(cache, request, interval=0.01)]

        assert [e for e in events if e.startswith("data: ")] == []

    async def test_skips_unchanged_version(self):
        """No new data event is sent while the cache version stays the same."""
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        request = _make_request(disconnected_after=3)

        events = [event async for event in _generate_events(cache, request, interval=0.01)]

        data_events = [e for e in events if e.startswith("data: ")]
        assert len(data_events) == 1

    async def test_stops_on_disconnect(self):
        cache = PriceCache()
        request = _make_request(disconnected_after=0)

        events = [event async for event in _generate_events(cache, request, interval=0.01)]

        assert events == ["retry: 1000\n\n"]


class TestCreateStreamRouter:
    def test_returns_a_new_router_each_call(self):
        """Regression test: the router must not be a shared module-level global."""
        cache = PriceCache()
        router1 = create_stream_router(cache)
        router2 = create_stream_router(cache)
        assert router1 is not router2

    def test_each_router_has_exactly_one_route(self):
        """Calling the factory twice must not accumulate duplicate routes."""
        cache = PriceCache()
        router1 = create_stream_router(cache)
        router2 = create_stream_router(cache)
        assert len(router1.routes) == 1
        assert len(router2.routes) == 1

    def test_route_path(self):
        cache = PriceCache()
        router = create_stream_router(cache)
        paths = [route.path for route in router.routes]
        assert "/api/stream/prices" in paths
