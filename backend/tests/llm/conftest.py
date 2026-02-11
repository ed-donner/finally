"""Fixtures for LLM service tests."""

import pytest

from app.db import init_db
from app.market.cache import PriceCache
from app.market.interface import MarketDataSource


class MockMarketDataSource(MarketDataSource):
    """Minimal mock market data source for testing."""

    def __init__(self):
        self.added_tickers: list[str] = []
        self.removed_tickers: list[str] = []

    async def start(self, tickers: list[str]) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def add_ticker(self, ticker: str) -> None:
        self.added_tickers.append(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self.removed_tickers.append(ticker)

    def get_tickers(self) -> list[str]:
        return []


@pytest.fixture
async def db(tmp_path):
    """Create an isolated database per test."""
    db_path = str(tmp_path / "test.db")
    conn = await init_db(db_path)
    yield conn
    await conn.close()


@pytest.fixture
def price_cache():
    """PriceCache with known prices for deterministic testing."""
    cache = PriceCache()
    cache.update("AAPL", 150.00)
    cache.update("GOOGL", 175.00)
    cache.update("MSFT", 400.00)
    cache.update("PYPL", 80.00)
    return cache


@pytest.fixture
def market_source():
    """Mock market data source that tracks add/remove calls."""
    return MockMarketDataSource()
