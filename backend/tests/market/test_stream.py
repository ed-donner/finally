"""Tests for the SSE streaming generator and route."""

import json
from types import SimpleNamespace

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


class _FakeRequest:
    """Minimal stand-in for a FastAPI Request.

    ``is_disconnected()`` returns False for the first ``disconnect_after``
    calls, then True — letting tests bound the generator loop deterministically.
    """

    def __init__(self, disconnect_after: int) -> None:
        self._calls = 0
        self._disconnect_after = disconnect_after
        self.client = SimpleNamespace(host="test-client")

    async def is_disconnected(self) -> bool:
        self._calls += 1
        return self._calls > self._disconnect_after


async def test_emits_retry_then_data_then_stops_on_disconnect():
    """The stream sends a retry directive, one data frame, then stops cleanly."""
    cache = PriceCache()
    cache.update("AAPL", 190.50, day_open=188.00)
    request = _FakeRequest(disconnect_after=1)

    events = [
        event
        async for event in _generate_events(cache, request, interval=0, heartbeat=1000)
    ]

    assert events[0] == "retry: 1000\n\n"
    assert events[1].startswith("data: ")

    payload = json.loads(events[1].removeprefix("data: ").strip())
    assert payload["AAPL"]["price"] == 190.50
    # The daily-change fields (C1) must be present in the SSE payload.
    assert payload["AAPL"]["day_open"] == 188.00
    assert "day_change_percent" in payload["AAPL"]

    # Disconnect after the first data frame → no further events.
    assert len(events) == 2


async def test_data_sent_once_then_heartbeat_when_version_unchanged():
    """When prices don't change, the stream emits keep-alive comments, not data."""
    cache = PriceCache()
    cache.update("AAPL", 190.50)
    request = _FakeRequest(disconnect_after=3)

    # heartbeat=0 → a keep-alive is due on any iteration with no new data.
    events = [
        event
        async for event in _generate_events(cache, request, interval=0, heartbeat=0)
    ]

    assert events[0] == "retry: 1000\n\n"
    assert events[1].startswith("data: ")  # initial snapshot, sent once
    # Subsequent iterations: version unchanged → keep-alive comments only.
    assert events[2] == ": keep-alive\n\n"
    assert all(not e.startswith("data: ") for e in events[2:])


def test_factory_returns_independent_routers():
    """Each create_stream_router() call yields a fresh router (N1).

    The route must be registered exactly once per router — not accumulated on a
    shared module-level router across calls.
    """
    cache = PriceCache()
    r1 = create_stream_router(cache)
    r2 = create_stream_router(cache)

    assert r1 is not r2
    paths1 = [r.path for r in r1.routes]
    paths2 = [r.path for r in r2.routes]
    assert paths1.count("/api/stream/prices") == 1
    assert paths2.count("/api/stream/prices") == 1


async def test_sse_route_sets_event_stream_headers():
    """The /prices route handler returns a correctly-configured SSE response (N3).

    Exercises the real route handler (the header-setting wrapper, not just the
    generator) by invoking its endpoint directly. We assert on the returned
    StreamingResponse without consuming the infinite body.
    """
    cache = PriceCache()
    cache.update("AAPL", 190.50, day_open=188.00)
    router = create_stream_router(cache)

    route = next(r for r in router.routes if getattr(r, "path", None) == "/api/stream/prices")
    request = _FakeRequest(disconnect_after=0)

    response = await route.endpoint(request)

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"
