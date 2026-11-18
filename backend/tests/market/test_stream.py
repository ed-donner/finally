"""Tests for the SSE price stream (T0.3, T0.4)."""

import asyncio
import json

import pytest
from fastapi import FastAPI

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


class FakeRequest:
    """Minimal stand-in for starlette's Request used by the generator."""

    client = None

    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


async def take(gen, n: int) -> list[str]:
    return [await asyncio.wait_for(gen.__anext__(), 1) for _ in range(n)]


@pytest.mark.asyncio
class TestGenerateEvents:
    async def test_first_frames_are_retry_then_data(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        gen = _generate_events(cache, FakeRequest(), interval=0.01)
        retry, data = await take(gen, 2)
        assert retry == "retry: 1000\n\n"
        payload = json.loads(data.removeprefix("data: "))
        assert payload["AAPL"]["price"] == 190.0
        assert set(payload["AAPL"]) == {
            "ticker", "price", "previous_price", "timestamp",
            "change", "change_percent", "direction",
        }
        await gen.aclose()

    async def test_emits_on_version_change(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        gen = _generate_events(cache, FakeRequest(), interval=0.01)
        await take(gen, 2)
        cache.update("AAPL", 191.0)
        (frame,) = await take(gen, 1)
        payload = json.loads(frame.removeprefix("data: "))
        assert payload["AAPL"]["direction"] == "up"
        await gen.aclose()

    async def test_empty_cache_after_removal_emits_empty_object(self):  # T0.3
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        gen = _generate_events(cache, FakeRequest(), interval=0.01)
        await take(gen, 2)
        cache.remove("AAPL")  # relies on T0.2 bumping the version
        assert await take(gen, 1) == ["data: {}\n\n"]
        await gen.aclose()

    async def test_never_populated_cache_sends_initial_empty_frame(self):
        gen = _generate_events(PriceCache(), FakeRequest(), interval=0.01)
        assert (await take(gen, 2))[1] == "data: {}\n\n"
        await gen.aclose()

    async def test_keepalive_when_idle(self):
        gen = _generate_events(PriceCache(), FakeRequest(), interval=0.01, keepalive_seconds=0.03)
        await take(gen, 2)
        assert await take(gen, 1) == [": keep-alive\n\n"]
        await gen.aclose()

    async def test_stops_on_disconnect(self):
        req = FakeRequest()
        gen = _generate_events(PriceCache(), req, interval=0.01)
        await take(gen, 2)
        req.disconnected = True
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), 1)


class TestCreateStreamRouter:
    def test_returns_fresh_router_each_call(self):  # T0.4
        r1 = create_stream_router(PriceCache())
        r2 = create_stream_router(PriceCache())
        assert r1 is not r2
        assert [r.path for r in r1.routes] == ["/api/stream/prices"]
        assert [r.path for r in r2.routes] == ["/api/stream/prices"]

    def test_two_apps_do_not_share_routes(self):
        a, b = FastAPI(), FastAPI()
        a.include_router(create_stream_router(PriceCache()))
        b.include_router(create_stream_router(PriceCache()))
        assert sum(getattr(r, "path", None) == "/api/stream/prices" for r in a.routes) == 1
        assert sum(getattr(r, "path", None) == "/api/stream/prices" for r in b.routes) == 1
