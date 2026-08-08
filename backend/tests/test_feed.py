import pytest

from market.cache import PriceCache
from market.feed import MarketFeed
from market.simulator import SimulatedSource
from market.source import MarketDataSource
from market.types import Bar


@pytest.mark.asyncio
async def test_feed_populates_cache_and_computes_direction():
    cache = PriceCache()
    feed = MarketFeed(SimulatedSource(seed=5), cache,
                      get_active_tickers=lambda: ["AAPL"])
    await feed.prime()          # first fill: previous == price, direction flat
    first = await cache.get("AAPL")
    assert first is not None and first.direction == "flat"

    await feed._tick_once()     # second step: direction reflects the move
    second = await cache.get("AAPL")
    assert second.previous_price == first.price
    assert second.direction in ("up", "down", "flat")


@pytest.mark.asyncio
async def test_feed_noop_when_no_active_tickers():
    cache = PriceCache()
    feed = MarketFeed(SimulatedSource(seed=5), cache, get_active_tickers=lambda: [])
    await feed.prime()
    assert await cache.snapshot() == {}


class _FlakySource(MarketDataSource):
    poll_interval_seconds = 0.01

    async def get_prices(self, tickers: list[str]) -> dict[str, float]:
        raise RuntimeError("boom")

    async def get_history(self, ticker: str, days: int = 90) -> list[Bar]:
        return []


@pytest.mark.asyncio
async def test_feed_survives_source_exception():
    cache = PriceCache()
    feed = MarketFeed(_FlakySource(), cache, get_active_tickers=lambda: ["AAPL"])
    await feed.prime()          # should not raise
    assert await cache.get("AAPL") is None


@pytest.mark.asyncio
async def test_feed_start_and_stop_cleans_up_task():
    cache = PriceCache()
    feed = MarketFeed(SimulatedSource(seed=1), cache, get_active_tickers=lambda: ["AAPL"])
    feed.start()
    await feed.stop()
    assert feed._task.cancelled() or feed._task.done()
