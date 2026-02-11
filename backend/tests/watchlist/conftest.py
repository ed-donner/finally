"""Fixtures for watchlist tests."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.db import init_db
from app.market.cache import PriceCache
from app.market.interface import MarketDataSource
from app.watchlist.router import create_watchlist_router


@pytest.fixture
async def db(tmp_path):
    """Create an isolated database per test."""
    db_path = str(tmp_path / "test.db")
    conn = await init_db(db_path)
    yield conn
    await conn.close()


class MockMarketDataSource(MarketDataSource):
    """Mock market data source that tracks add/remove calls."""

    def __init__(self):
        self.added: list[str] = []
        self.removed: list[str] = []

    async def start(self, tickers: list[str]) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def add_ticker(self, ticker: str) -> None:
        self.added.append(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self.removed.append(ticker)

    def get_tickers(self) -> list[str]:
        return []


@pytest.fixture
def mock_market_data_source():
    """Return a mock market data source for tracking add/remove calls."""
    return MockMarketDataSource()


@pytest.fixture
def price_cache():
    """Return a PriceCache pre-seeded with a couple of prices."""
    cache = PriceCache()
    cache.update("AAPL", 190.50)
    cache.update("GOOGL", 175.25)
    return cache


@pytest.fixture
async def client(db, price_cache, mock_market_data_source):
    """Async HTTP client wired to a test FastAPI app with watchlist router."""
    app = FastAPI()
    router = create_watchlist_router(db, price_cache, mock_market_data_source)
    app.include_router(router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
