"""Unit tests for PriceCache."""
import asyncio
from datetime import datetime, timezone
import pytest
from app.market.cache import PriceCache
from app.market.models import PriceUpdate


def make_update(ticker: str, price: float, prev: float = 0.0) -> PriceUpdate:
    change = price - prev
    return PriceUpdate(
        ticker=ticker,
        price=price,
        prev_price=prev,
        timestamp=datetime.now(tz=timezone.utc),
        change=change,
        change_pct=(change / prev * 100) if prev else 0.0,
    )


@pytest.mark.asyncio
async def test_get_returns_none_for_unknown_ticker():
    cache = PriceCache()
    assert cache.get("AAPL") is None


@pytest.mark.asyncio
async def test_update_stores_and_retrieves_price():
    cache = PriceCache()
    update = make_update("AAPL", 190.0, 189.0)
    await cache.update([update])
    result = cache.get("AAPL")
    assert result is not None
    assert result.price == 190.0


@pytest.mark.asyncio
async def test_update_overwrites_existing_price():
    cache = PriceCache()
    await cache.update([make_update("AAPL", 190.0)])
    await cache.update([make_update("AAPL", 191.5)])
    assert cache.get("AAPL").price == 191.5


@pytest.mark.asyncio
async def test_get_all_returns_all_tickers():
    cache = PriceCache()
    await cache.update([
        make_update("AAPL", 190.0),
        make_update("MSFT", 415.0),
        make_update("GOOGL", 175.0),
    ])
    all_prices = cache.get_all()
    assert set(all_prices.keys()) == {"AAPL", "MSFT", "GOOGL"}


@pytest.mark.asyncio
async def test_get_all_returns_copy():
    cache = PriceCache()
    await cache.update([make_update("AAPL", 190.0)])
    snapshot = cache.get_all()
    await cache.update([make_update("AAPL", 200.0)])
    # original snapshot should be unchanged
    assert snapshot["AAPL"].price == 190.0


@pytest.mark.asyncio
async def test_get_tickers_returns_list():
    cache = PriceCache()
    await cache.update([make_update("AAPL", 190.0), make_update("TSLA", 250.0)])
    tickers = cache.get_tickers()
    assert sorted(tickers) == ["AAPL", "TSLA"]


@pytest.mark.asyncio
async def test_subscribe_receives_updates():
    cache = PriceCache()
    queue = cache.subscribe()
    update = make_update("NVDA", 875.0)
    await cache.update([update])
    received = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert received.ticker == "NVDA"
    assert received.price == 875.0


@pytest.mark.asyncio
async def test_multiple_subscribers_each_receive_updates():
    cache = PriceCache()
    q1 = cache.subscribe()
    q2 = cache.subscribe()
    await cache.update([make_update("JPM", 200.0)])
    r1 = await asyncio.wait_for(q1.get(), timeout=1.0)
    r2 = await asyncio.wait_for(q2.get(), timeout=1.0)
    assert r1.ticker == "JPM"
    assert r2.ticker == "JPM"


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    cache = PriceCache()
    queue = cache.subscribe()
    cache.unsubscribe(queue)
    await cache.update([make_update("AAPL", 190.0)])
    # Queue should remain empty after unsubscribe
    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()


@pytest.mark.asyncio
async def test_unsubscribe_nonexistent_queue_is_safe():
    cache = PriceCache()
    queue: asyncio.Queue = asyncio.Queue()
    cache.unsubscribe(queue)  # should not raise


@pytest.mark.asyncio
async def test_update_multiple_tickers_at_once():
    cache = PriceCache()
    queue = cache.subscribe()
    updates = [make_update("AAPL", 190.0), make_update("MSFT", 415.0)]
    await cache.update(updates)
    received = []
    for _ in range(2):
        received.append(await asyncio.wait_for(queue.get(), timeout=1.0))
    tickers_received = {r.ticker for r in received}
    assert tickers_received == {"AAPL", "MSFT"}
