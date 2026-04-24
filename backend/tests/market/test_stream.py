"""Tests for the SSE streaming endpoint."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


def _make_request(disconnect_after: int = 2) -> MagicMock:
    """Build a mock Request that disconnects after `disconnect_after` checks."""
    request = MagicMock()
    request.client.host = "127.0.0.1"
    call_count = 0

    async def is_disconnected() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count > disconnect_after

    request.is_disconnected = is_disconnected
    return request


async def _collect(gen, max_items: int = 20) -> list[str]:
    """Collect up to max_items from an async generator."""
    items = []
    async for item in gen:
        items.append(item)
        if len(items) >= max_items:
            break
    return items


class TestCreateStreamRouter:

    def test_returns_router_with_prices_route(self):
        """create_stream_router returns a router that has /api/stream/prices."""
        cache = PriceCache()
        router = create_stream_router(cache)
        routes = [r.path for r in router.routes]
        assert "/api/stream/prices" in routes

    def test_returns_fresh_router_each_call(self):
        """Calling create_stream_router twice returns independent routers."""
        cache = PriceCache()
        r1 = create_stream_router(cache)
        r2 = create_stream_router(cache)
        assert r1 is not r2

    def test_no_duplicate_routes_on_multiple_calls(self):
        """Multiple calls do not accumulate duplicate routes."""
        cache = PriceCache()
        r1 = create_stream_router(cache)
        r2 = create_stream_router(cache)
        assert len(r1.routes) == len(r2.routes) == 1


@pytest.mark.asyncio
class TestGenerateEvents:

    async def test_first_event_is_retry_directive(self):
        """Generator opens with a retry: directive for EventSource reconnection."""
        cache = PriceCache()
        request = _make_request(disconnect_after=1)
        events = await _collect(_generate_events(cache, request, interval=0.01))
        assert events[0] == "retry: 1000\n\n"

    async def test_yields_one_event_per_ticker(self):
        """Each ticker gets its own SSE event (not batched)."""
        cache = PriceCache()
        cache.update("AAPL", 190.50, open_price=190.00)
        cache.update("GOOGL", 175.25, open_price=175.00)

        request = _make_request(disconnect_after=3)
        events = await _collect(_generate_events(cache, request, interval=0.01))

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) == 2
        tickers = {json.loads(e[len("data: "):-2])["ticker"] for e in data_events}
        assert tickers == {"AAPL", "GOOGL"}

    async def test_event_contains_required_fields(self):
        """Each SSE event has all fields required by PLAN.md §6."""
        cache = PriceCache()
        cache.update("AAPL", 190.50, open_price=190.00)

        request = _make_request(disconnect_after=2)
        events = await _collect(_generate_events(cache, request, interval=0.01))

        data_events = [e for e in events if e.startswith("data:")]
        assert data_events, "Expected at least one data event"

        payload = json.loads(data_events[0][len("data: "):-2])
        for field in ("ticker", "price", "prev_price", "open_price", "timestamp", "direction"):
            assert field in payload, f"Missing field: {field}"

    async def test_timestamp_is_iso_string(self):
        """SSE timestamp must be ISO 8601, not a Unix float."""
        cache = PriceCache()
        cache.update("AAPL", 190.50, open_price=190.00)

        request = _make_request(disconnect_after=2)
        events = await _collect(_generate_events(cache, request, interval=0.01))

        data_events = [e for e in events if e.startswith("data:")]
        payload = json.loads(data_events[0][len("data: "):-2])
        ts = payload["timestamp"]
        assert isinstance(ts, str)
        assert "T" in ts
        assert ts.endswith("Z")

    async def test_open_price_in_event(self):
        """open_price is present in SSE events so frontend can compute daily change %."""
        cache = PriceCache()
        cache.update("AAPL", 192.00, open_price=190.00)

        request = _make_request(disconnect_after=2)
        events = await _collect(_generate_events(cache, request, interval=0.01))

        data_events = [e for e in events if e.startswith("data:")]
        payload = json.loads(data_events[0][len("data: "):-2])
        assert payload["open_price"] == 190.00

    async def test_stops_on_disconnect(self):
        """Generator terminates when request.is_disconnected() returns True."""
        cache = PriceCache()
        cache.update("AAPL", 190.00, open_price=190.00)

        request = _make_request(disconnect_after=1)
        events = await _collect(_generate_events(cache, request, interval=0.01), max_items=100)
        # Should stop; not infinite
        assert len(events) < 100

    async def test_skips_send_when_cache_unchanged(self):
        """Version-based deduplication: no data event when cache has not changed."""
        cache = PriceCache()
        cache.update("AAPL", 190.00, open_price=190.00)

        # Force disconnect after 4 is_disconnected calls but use a request
        # that we can count events on — with fast interval, if no cache update
        # happens, the second pass should not yield a new data event.
        call_count = 0

        async def is_disconnected():
            nonlocal call_count
            call_count += 1
            return call_count > 4

        request = MagicMock()
        request.client.host = "test"
        request.is_disconnected = is_disconnected

        events = await _collect(_generate_events(cache, request, interval=0.01), max_items=50)
        data_events = [e for e in events if e.startswith("data:")]
        # Only one pass should produce data events (cache version only changed once)
        assert len(data_events) == 1

    async def test_empty_cache_yields_no_data_events(self):
        """No data events when cache is empty."""
        cache = PriceCache()  # empty

        request = _make_request(disconnect_after=2)
        events = await _collect(_generate_events(cache, request, interval=0.01))

        data_events = [e for e in events if e.startswith("data:")]
        assert data_events == []
