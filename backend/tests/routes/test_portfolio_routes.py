"""HTTP-level tests for portfolio API endpoints."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.db import init_db
from app.market.cache import PriceCache
from app.routes.portfolio import create_portfolio_router


@pytest.fixture
async def app(tmp_path):
    """FastAPI app with portfolio router and isolated test database."""
    db_path = str(tmp_path / "test.db")
    db = await init_db(db_path)

    price_cache = PriceCache()
    price_cache.update("AAPL", 150.00)
    price_cache.update("GOOGL", 175.00)

    application = FastAPI()
    application.include_router(create_portfolio_router(db, price_cache))

    yield application
    await db.close()


@pytest.fixture
async def client(app):
    """Async HTTP client for testing endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_get_portfolio_empty(client):
    """GET /api/portfolio with no positions returns cash only."""
    resp = await client.get("/api/portfolio")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cash_balance"] == 10000.0
    assert data["positions"] == []
    assert data["total_value"] == 10000.0


async def test_post_trade_buy(client):
    """POST /api/portfolio/trade buy returns trade details."""
    resp = await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "side": "buy", "quantity": 10},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ticker"] == "AAPL"
    assert data["side"] == "buy"
    assert data["quantity"] == 10
    assert data["price"] == 150.0
    assert data["total"] == 1500.0


async def test_post_trade_buy_updates_portfolio(client):
    """After buying, portfolio reflects updated cash and position."""
    await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "side": "buy", "quantity": 10},
    )
    resp = await client.get("/api/portfolio")
    data = resp.json()
    assert data["cash_balance"] == 8500.0
    assert len(data["positions"]) == 1
    assert data["positions"][0]["ticker"] == "AAPL"
    assert data["positions"][0]["quantity"] == 10
    assert data["total_value"] == 10000.0


async def test_post_trade_sell_after_buy(client):
    """Sell after buy returns correct trade details."""
    await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "side": "buy", "quantity": 10},
    )
    resp = await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "side": "sell", "quantity": 5},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ticker"] == "AAPL"
    assert data["side"] == "sell"
    assert data["quantity"] == 5
    assert data["price"] == 150.0
    assert data["total"] == 750.0


async def test_post_trade_insufficient_cash(client):
    """Buy exceeding cash returns 400."""
    resp = await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "side": "buy", "quantity": 100},
    )
    assert resp.status_code == 400
    assert "Insufficient cash" in resp.json()["detail"]


async def test_post_trade_insufficient_shares(client):
    """Sell without owning returns 400."""
    resp = await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "side": "sell", "quantity": 10},
    )
    assert resp.status_code == 400
    assert "Insufficient shares" in resp.json()["detail"]


async def test_post_trade_invalid_side(client):
    """Invalid side value returns 422."""
    resp = await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "side": "short", "quantity": 10},
    )
    assert resp.status_code == 422


async def test_post_trade_negative_quantity(client):
    """Negative quantity returns 422."""
    resp = await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "side": "buy", "quantity": -5},
    )
    assert resp.status_code == 422


async def test_post_trade_missing_fields(client):
    """Missing required fields returns 422."""
    resp = await client.post("/api/portfolio/trade", json={})
    assert resp.status_code == 422


async def test_get_portfolio_history_empty(client):
    """GET /api/portfolio/history with no snapshots returns empty list."""
    resp = await client.get("/api/portfolio/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["snapshots"] == []


async def test_trade_creates_snapshot(client):
    """Each trade should create an immediate portfolio snapshot."""
    await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "side": "buy", "quantity": 10},
    )
    resp = await client.get("/api/portfolio/history")
    data = resp.json()
    assert len(data["snapshots"]) >= 1
    assert data["snapshots"][0]["total_value"] == 10000.0
