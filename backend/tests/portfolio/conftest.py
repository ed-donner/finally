"""Fixtures for portfolio service tests."""

import pytest

from app.db import init_db
from app.market.cache import PriceCache


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
    return cache
