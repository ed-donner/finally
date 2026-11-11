"""Conformance tests: every MarketDataSource implementation must satisfy the same contract."""

import asyncio

import pytest

from app.market.cache import PriceCache
from app.market.interface import MarketDataSource, normalize_ticker
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource

from .fixtures import FakeMassiveClient, snapshot_for_all


@pytest.fixture(params=["simulator", "massive"])
async def source(request):
    cache = PriceCache()
    if request.param == "simulator":
        src: MarketDataSource = SimulatorDataSource(cache, update_interval=0.01, seed=1)
    else:
        fake = FakeMassiveClient(snapshot=snapshot_for_all)
        src = MassiveDataSource("key", cache, poll_interval=0.01, client=fake)
    yield src, cache
    await src.stop()


@pytest.mark.parametrize("raw,expected", [(" aapl ", "AAPL"), ("brk.b", "BRK.B"), ("MSFT", "MSFT")])
def test_normalize_ticker(raw, expected):
    assert normalize_ticker(raw) == expected


def test_abc_cannot_be_instantiated():
    with pytest.raises(TypeError):
        MarketDataSource()  # type: ignore[abstract]


async def test_start_normalizes_dedupes_and_prices(source):  # C1, C2
    src, cache = source
    await src.start([" aapl", "MSFT", "AAPL", "  "])
    assert src.get_tickers() == ["AAPL", "MSFT"]
    assert cache.get_price("AAPL") is not None
    assert cache.get_price("MSFT") is not None


async def test_add_is_idempotent_and_normalized(source):  # C1, C3
    src, cache = source
    await src.start(["AAPL"])
    await src.add_ticker("tsla")
    await src.add_ticker("TSLA ")
    assert src.get_tickers() == ["AAPL", "TSLA"]
    await asyncio.sleep(0.05)  # Massive prices it on the next poll
    assert cache.get_price("TSLA") is not None


async def test_remove_evicts_and_is_idempotent(source):  # C4
    src, cache = source
    await src.start(["AAPL", "MSFT"])
    await src.remove_ticker("aapl")
    await src.remove_ticker("AAPL")
    assert src.get_tickers() == ["MSFT"]
    assert "AAPL" not in cache
    await asyncio.sleep(0.05)
    assert "AAPL" not in cache


async def test_set_tickers_reconciles(source):
    src, _ = source
    await src.start(["AAPL", "MSFT"])
    await src.set_tickers({"msft", "TSLA", ""})
    assert sorted(src.get_tickers()) == ["MSFT", "TSLA"]


async def test_stop_is_idempotent_and_safe_before_start(source):  # C5
    src, cache = source
    await src.stop()
    await src.start(["AAPL"])
    await src.stop()
    await src.stop()
    v = cache.version
    await asyncio.sleep(0.05)
    assert cache.version == v


async def test_empty_start(source):
    src, cache = source
    await src.start([])
    assert src.get_tickers() == []
    assert len(cache) == 0
