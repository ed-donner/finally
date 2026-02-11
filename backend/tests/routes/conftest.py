"""Fixtures for route tests."""


import pytest
from httpx import ASGITransport, AsyncClient

from app.db import init_db, set_db_path
from app.market import PriceCache


@pytest.fixture
async def test_db(tmp_path):
    """Create a temporary test database."""
    db_path = str(tmp_path / "test.db")
    set_db_path(db_path)
    await init_db()
    yield db_path
    # Reset to default after test
    set_db_path(str(tmp_path / "unused.db"))


@pytest.fixture
def price_cache():
    """A PriceCache with some test prices."""
    cache = PriceCache()
    cache.update("AAPL", 190.50)
    cache.update("GOOGL", 175.25)
    cache.update("MSFT", 420.00)
    return cache


@pytest.fixture
async def client(test_db, price_cache):
    """Async HTTP client wired to the FastAPI app, bypassing lifespan."""
    from fastapi import FastAPI

    from app.routes.chat import router as chat_router
    from app.routes.portfolio import router as portfolio_router
    from app.routes.watchlist import router as watchlist_router

    # Build a test app without the full lifespan (no market data source needed)
    test_app = FastAPI()
    test_app.include_router(portfolio_router)
    test_app.include_router(watchlist_router)
    test_app.include_router(chat_router)

    @test_app.get("/api/health")
    async def health():
        return {"status": "ok"}

    # Mock market source
    class MockSource:
        async def add_ticker(self, ticker): pass
        async def remove_ticker(self, ticker): pass

    test_app.state.price_cache = price_cache
    test_app.state.market_source = MockSource()

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
