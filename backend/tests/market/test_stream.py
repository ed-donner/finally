"""Integration tests for SSE streaming endpoint."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.responses import StreamingResponse

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


class TestGenerateEvents:
    """Tests for the _generate_events async generator."""

    @pytest.mark.asyncio
    async def test_retry_directive_is_first(self):
        """First yielded chunk should be the SSE retry directive."""
        cache = PriceCache()
        mock_request = MagicMock()
        mock_request.client.host = "test"
        mock_request.is_disconnected = AsyncMock(return_value=True)

        events = []
        async for event in _generate_events(cache, mock_request, interval=0.0):
            events.append(event)

        assert events[0] == "retry: 1000\n\n"

    @pytest.mark.asyncio
    async def test_data_event_json_structure(self):
        """Data events should contain valid JSON with expected fields."""
        cache = PriceCache()
        cache.update("AAPL", 190.0)

        mock_request = MagicMock()
        mock_request.client.host = "test"
        # First check: not disconnected (yields data); second: disconnected (stops)
        mock_request.is_disconnected = AsyncMock(side_effect=[False, True])

        events = []
        async for event in _generate_events(cache, mock_request, interval=0.0):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) >= 1

        payload = json.loads(data_events[0][len("data: "):])
        assert "AAPL" in payload
        aapl = payload["AAPL"]
        for key in ("ticker", "price", "previous_price", "timestamp", "direction"):
            assert key in aapl, f"Expected key '{key}' in AAPL payload"

    @pytest.mark.asyncio
    async def test_no_data_event_when_cache_empty(self):
        """No data event should be emitted when the cache is empty."""
        cache = PriceCache()
        mock_request = MagicMock()
        mock_request.client.host = "test"
        mock_request.is_disconnected = AsyncMock(side_effect=[False, True])

        events = []
        async for event in _generate_events(cache, mock_request, interval=0.0):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) == 0

    @pytest.mark.asyncio
    async def test_stops_on_immediate_disconnect(self):
        """Generator stops immediately when client is already disconnected."""
        cache = PriceCache()
        cache.update("AAPL", 190.0)

        mock_request = MagicMock()
        mock_request.client.host = "test"
        mock_request.is_disconnected = AsyncMock(return_value=True)

        events = []
        async for event in _generate_events(cache, mock_request, interval=0.0):
            events.append(event)

        # Only the retry directive should be emitted; no data loop runs
        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) == 0


class TestStreamRouterFactory:
    """Tests for create_stream_router."""

    def test_fresh_router_each_call(self):
        """Each call returns a distinct APIRouter instance (no double-registration risk)."""
        cache = PriceCache()
        router1 = create_stream_router(cache)
        router2 = create_stream_router(cache)
        assert router1 is not router2

    def test_router_has_prices_route(self):
        """Router must expose the /prices route (path includes the prefix)."""
        cache = PriceCache()
        router = create_stream_router(cache)
        paths = [r.path for r in router.routes]
        assert "/api/stream/prices" in paths


class TestSSEEndpoint:
    """Tests for the /api/stream/prices route handler."""

    @pytest.mark.asyncio
    async def test_handler_returns_streaming_response(self):
        """Route handler must return a StreamingResponse with correct headers."""
        cache = PriceCache()
        cache.update("AAPL", 190.0)

        router = create_stream_router(cache)
        # Extract the registered route handler
        route = router.routes[0]

        mock_request = MagicMock()
        mock_request.client.host = "test"
        mock_request.is_disconnected = AsyncMock(return_value=True)

        response = await route.endpoint(mock_request)

        assert isinstance(response, StreamingResponse)
        assert response.media_type == "text/event-stream"
        assert response.headers.get("cache-control") == "no-cache"
        assert response.headers.get("x-accel-buffering") == "no"
