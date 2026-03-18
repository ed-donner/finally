"""Fixtures for API tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.connection import set_db_path
from app.db.init_db import init_db
from app.market import PriceCache
from app.main import create_app


@pytest.fixture
async def price_cache():
    """A fresh PriceCache with some seeded prices."""
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    cache.update("GOOGL", 175.0)
    cache.update("MSFT", 420.0)
    cache.update("TSLA", 250.0)
    return cache


class FakeMarketSource:
    """Minimal stub that satisfies the MarketDataSource interface for tests."""

    def __init__(self):
        self.tickers = set()

    async def start(self, tickers):
        self.tickers = set(tickers)

    async def stop(self):
        pass

    async def add_ticker(self, ticker):
        self.tickers.add(ticker)

    async def remove_ticker(self, ticker):
        self.tickers.discard(ticker)

    def get_tickers(self):
        return sorted(self.tickers)


@pytest.fixture
async def app_client(tmp_path, price_cache):
    """AsyncClient wired to a test FastAPI app with a fresh DB."""
    db_file = str(tmp_path / "test.db")
    set_db_path(db_file)
    await init_db()

    app = create_app()
    app.state.price_cache = price_cache
    app.state.market_source = FakeMarketSource()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    set_db_path(None)
