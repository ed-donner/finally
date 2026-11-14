"""Tests for SimulatorDataSource (the async adapter around GBMSimulator)."""

import asyncio
import logging

import pytest

from app.market.cache import PriceCache
from app.market.seed_prices import SEED_PRICES
from app.market.simulator import SimulatorDataSource


@pytest.fixture
async def running():
    cache = PriceCache()
    source = SimulatorDataSource(cache, update_interval=0.01, seed=1)
    await source.start(["AAPL", "MSFT"])
    yield source, cache
    await source.stop()


async def test_start_seeds_cache_with_reference(running):
    _, cache = running
    u = cache.get("AAPL")
    assert u is not None
    assert u.reference_price == SEED_PRICES["AAPL"]


async def test_loop_updates_prices(running):
    _, cache = running
    v = cache.version
    await asyncio.sleep(0.08)
    assert cache.version > v


async def test_reference_survives_ticks(running):
    _, cache = running
    await asyncio.sleep(0.05)
    u = cache.get("AAPL")
    assert u.reference_price == SEED_PRICES["AAPL"]
    assert u.day_change_percent is not None


async def test_add_ticker_prices_immediately(running):
    source, cache = running
    await source.add_ticker(" pypl ")
    assert "PYPL" in source.get_tickers()
    u = cache.get("PYPL")
    assert u is not None and u.reference_price == u.price


async def test_add_existing_does_not_reset_reference(running):
    source, cache = running
    await asyncio.sleep(0.05)
    before = cache.get("AAPL").reference_price
    await source.add_ticker("aapl")
    assert cache.get("AAPL").reference_price == before
    assert source.get_tickers().count("AAPL") == 1


async def test_remove_ticker(running):
    source, cache = running
    await source.remove_ticker("msft")
    assert "MSFT" not in source.get_tickers()
    assert "MSFT" not in cache
    await asyncio.sleep(0.03)
    assert "MSFT" not in cache  # loop doesn't resurrect it


async def test_initial_prices_used():
    cache = PriceCache()
    source = SimulatorDataSource(cache, initial_prices={"pypl": 71.5})
    await source.start(["PYPL"])
    try:
        assert cache.get_price("PYPL") == 71.5
        assert cache.get("PYPL").reference_price == 71.5
    finally:
        await source.stop()


async def test_add_before_start_is_noop():
    source = SimulatorDataSource(PriceCache())
    await source.add_ticker("AAPL")
    assert source.get_tickers() == []


async def test_stop_halts_updates():
    cache = PriceCache()
    source = SimulatorDataSource(cache, update_interval=0.01)
    await source.start(["AAPL"])
    await source.stop()
    v = cache.version
    await asyncio.sleep(0.05)
    assert cache.version == v


async def test_loop_survives_step_errors(caplog):
    cache = PriceCache()
    source = SimulatorDataSource(cache, update_interval=0.01)
    await source.start(["AAPL"])
    calls = {"n": 0}
    real_step = source._sim.step

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real_step()

    source._sim.step = flaky
    v = cache.version
    with caplog.at_level(logging.ERROR):
        await asyncio.sleep(0.08)
    await source.stop()
    assert "Simulator step failed" in caplog.text
    assert cache.version > v


async def test_event_probability_passed_through():
    cache = PriceCache()
    source = SimulatorDataSource(cache, update_interval=0.01, event_probability=1.0, seed=3)
    await source.start(["AAPL"])
    await asyncio.sleep(0.05)
    await source.stop()
    assert abs(cache.get_price("AAPL") / SEED_PRICES["AAPL"] - 1) > 0.019
