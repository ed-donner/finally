"""Tests for the SSE price stream."""

import json

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from app.market import PriceCache, create_stream_router
from app.market.stream import generate_price_events

from .fixtures import FakeRequest


async def collect(cache, disconnect_after, **kw):
    return [e async for e in generate_price_events(cache, FakeRequest(disconnect_after), **kw)]


def data_events(events):
    return [json.loads(e.split("data: ", 1)[1]) for e in events if e.startswith("id:")]


async def test_first_frame_is_retry_directive():
    events = await collect(PriceCache(), 1, interval=0)
    assert events == ["retry: 1000\n\n"]


async def test_sends_full_snapshot_immediately():
    cache = PriceCache()
    cache.update("AAPL", 190.0, reference_price=190.0)
    cache.update("MSFT", 420.0)
    events = await collect(cache, 2, interval=0)
    assert events[1].startswith(f"id: {cache.version}\ndata: ")
    assert events[1].endswith("\n\n")
    [payload] = data_events(events)
    assert set(payload) == {"AAPL", "MSFT"}
    assert payload["AAPL"]["day_change_percent"] == 0.0
    assert payload["AAPL"]["direction"] == "flat"
    assert payload["MSFT"]["reference_price"] is None


async def test_skips_when_unchanged():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    events = await collect(cache, 5, interval=0, heartbeat=3600)
    assert len(data_events(events)) == 1


async def test_sends_again_after_change():
    cache = PriceCache()
    cache.update("AAPL", 190.0)

    class ChangingRequest(FakeRequest):
        async def is_disconnected(self):
            if self._remaining == 2:
                cache.update("AAPL", 191.0)
            return await super().is_disconnected()

    events = [e async for e in generate_price_events(cache, ChangingRequest(3), interval=0)]
    payloads = data_events(events)
    assert [p["AAPL"]["price"] for p in payloads] == [190.0, 191.0]
    assert payloads[1]["AAPL"]["direction"] == "up"


async def test_removal_is_pushed():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    cache.update("MSFT", 420.0)

    class RemovingRequest(FakeRequest):
        async def is_disconnected(self):
            if self._remaining == 2:
                cache.remove("MSFT")
            return await super().is_disconnected()

    events = [e async for e in generate_price_events(cache, RemovingRequest(3), interval=0)]
    assert [set(p) for p in data_events(events)] == [{"AAPL", "MSFT"}, {"AAPL"}]


async def test_empty_cache_sends_empty_object():
    events = await collect(PriceCache(), 2, interval=0)
    assert data_events(events) == [{}]


async def test_heartbeat_when_idle():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    events = await collect(cache, 3, interval=0, heartbeat=0)
    assert ": keepalive\n\n" in events


def test_router_factory_is_reentrant():
    r1 = create_stream_router(PriceCache())
    r2 = create_stream_router(PriceCache())
    assert len(r1.routes) == 1
    assert len(r2.routes) == 1
    assert r1.routes[0].path == "/api/stream/prices"


async def test_endpoint_returns_event_stream():
    app = FastAPI()
    app.include_router(create_stream_router(PriceCache()))
    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/stream/prices")
    resp = await route.endpoint(FakeRequest(1))
    assert isinstance(resp, StreamingResponse)
    assert resp.media_type == "text/event-stream"
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-accel-buffering"] == "no"
