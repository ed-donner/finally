import pytest

from market.cache import PriceCache
from market.types import PriceTick


def _tick(ticker: str, price: float) -> PriceTick:
    return PriceTick(ticker=ticker, price=price, previous_price=price,
                      direction="flat", timestamp="2026-08-08T00:00:00+00:00")


@pytest.mark.asyncio
async def test_get_missing_ticker_returns_none():
    cache = PriceCache()
    assert await cache.get("AAPL") is None


@pytest.mark.asyncio
async def test_update_then_get_roundtrips():
    cache = PriceCache()
    await cache.update(_tick("AAPL", 190.0))
    tick = await cache.get("AAPL")
    assert tick is not None and tick.price == 190.0


@pytest.mark.asyncio
async def test_snapshot_is_a_copy_not_a_live_view():
    cache = PriceCache()
    await cache.update(_tick("AAPL", 190.0))
    snap = await cache.snapshot()
    await cache.update(_tick("AAPL", 200.0))
    assert snap["AAPL"].price == 190.0          # snapshot unaffected by later writes


@pytest.mark.asyncio
async def test_snapshot_contains_all_tickers():
    cache = PriceCache()
    await cache.update(_tick("AAPL", 190.0))
    await cache.update(_tick("MSFT", 420.0))
    snap = await cache.snapshot()
    assert set(snap) == {"AAPL", "MSFT"}
