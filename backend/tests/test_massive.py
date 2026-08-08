import httpx
import pytest

from market.massive import MassiveSource

SNAPSHOT = {
    "tickers": [
        {"ticker": "AAPL", "lastTrade": {"p": 191.2},
         "day": {"c": 190.0}, "prevDay": {"c": 189.0}},
        {"ticker": "MSFT", "lastTrade": {},          # closed: fall back to day.c
         "day": {"c": 421.5}, "prevDay": {"c": 420.0}},
    ]
}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="https://api.massive.com")


@pytest.mark.asyncio
async def test_snapshot_parsing_and_fallback():
    src = MassiveSource("key")
    src._client = _client(lambda req: httpx.Response(200, json=SNAPSHOT))
    prices = await src.get_prices(["AAPL", "MSFT"])
    assert prices == {"AAPL": 191.2, "MSFT": 421.5}      # last trade, then day.c
    await src.aclose()


@pytest.mark.asyncio
async def test_empty_tickers_short_circuits_without_request():
    called = False

    def handler(req):
        nonlocal called
        called = True
        return httpx.Response(200, json={"tickers": []})

    src = MassiveSource("key")
    src._client = _client(handler)
    assert await src.get_prices([]) == {}
    assert called is False
    await src.aclose()


@pytest.mark.asyncio
async def test_rate_limit_returns_empty_not_raises():
    src = MassiveSource("key")
    src._client = _client(lambda req: httpx.Response(429, json={}))
    assert await src.get_prices(["AAPL"]) == {}          # feed keeps cached values
    await src.aclose()


@pytest.mark.asyncio
async def test_auth_error_returns_empty_not_raises():
    src = MassiveSource("key")
    src._client = _client(lambda req: httpx.Response(401, json={}))
    assert await src.get_prices(["AAPL"]) == {}
    await src.aclose()


@pytest.mark.asyncio
async def test_transport_error_returns_empty_not_raises():
    def handler(req):
        raise httpx.ConnectError("boom", request=req)

    src = MassiveSource("key")
    src._client = _client(handler)
    assert await src.get_prices(["AAPL"]) == {}
    await src.aclose()


@pytest.mark.asyncio
async def test_history_maps_to_bar_shape():
    aggs = {"results": [{"t": 1_700_000_000_000, "o": 1, "h": 2, "l": 0.5,
                         "c": 1.5, "v": 100}]}
    src = MassiveSource("key")
    src._client = _client(lambda req: httpx.Response(200, json=aggs))
    bars = await src.get_history("AAPL", days=1)
    assert bars[0].o == 1 and bars[0].low == 0.5 and bars[0].c == 1.5
    await src.aclose()


@pytest.mark.asyncio
async def test_history_transport_error_returns_empty_list():
    def handler(req):
        raise httpx.ConnectError("boom", request=req)

    src = MassiveSource("key")
    src._client = _client(handler)
    assert await src.get_history("AAPL", days=5) == []
    await src.aclose()


@pytest.mark.asyncio
async def test_poll_interval_is_configurable():
    src = MassiveSource("key", poll_interval_seconds=3.0)
    assert src.poll_interval_seconds == 3.0
    await src.aclose()
