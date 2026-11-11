"""Tests for create_market_data_source."""

import pytest

from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    monkeypatch.delenv("MASSIVE_POLL_INTERVAL", raising=False)


def test_no_key_gives_simulator():
    assert isinstance(create_market_data_source(PriceCache()), SimulatorDataSource)


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_blank_key_gives_simulator(monkeypatch, value):
    monkeypatch.setenv("MASSIVE_API_KEY", value)
    assert isinstance(create_market_data_source(PriceCache()), SimulatorDataSource)


def test_key_gives_massive_with_stripped_key(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "  abc123 ")
    cache = PriceCache()
    src = create_market_data_source(cache)
    assert isinstance(src, MassiveDataSource)
    assert src._api_key == "abc123"
    assert src._cache is cache
    assert src._snapshot_interval == 15.0


@pytest.mark.parametrize("raw,expected", [("5", 5.0), ("2.5", 2.5), ("0.1", 1.0), ("abc", 15.0)])
def test_poll_interval_env(monkeypatch, raw, expected):
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    monkeypatch.setenv("MASSIVE_POLL_INTERVAL", raw)
    assert create_market_data_source(PriceCache())._snapshot_interval == expected


async def test_initial_prices_reach_simulator():
    cache = PriceCache()
    src = create_market_data_source(cache, initial_prices={"PYPL": 71.5})
    await src.start(["PYPL"])
    await src.stop()
    assert cache.get_price("PYPL") == 71.5


def test_source_is_unstarted():
    src = create_market_data_source(PriceCache())
    assert src.get_tickers() == []
