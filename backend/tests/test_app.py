"""Integration tests for the assembled FinAlly application."""

import importlib
import pathlib

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(tmp_path, monkeypatch):
    """Full app client with lifespan, isolated DB, and real static files."""
    db_path = str(tmp_path / "test.db")
    static_dir = str(pathlib.Path(__file__).parent.parent / "static")

    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("STATIC_DIR", static_dir)
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)

    import app.main as main_mod

    importlib.reload(main_mod)
    application = main_mod.app

    async with LifespanManager(application) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_health(client):
    """GET /api/health returns 200 with healthy status."""
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


async def test_watchlist_loaded(client):
    """Watchlist loads 10 default seeded tickers on startup."""
    resp = await client.get("/api/watchlist")
    assert resp.status_code == 200
    assert resp.json()["count"] == 10


async def test_portfolio_initial(client):
    """Initial portfolio has $10,000 cash and no positions."""
    resp = await client.get("/api/portfolio")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cash_balance"] == 10000.0


async def test_trade_through_assembled_app(client):
    """Buy trade reduces cash and creates a position."""
    resp = await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "side": "buy", "quantity": 5},
    )
    assert resp.status_code == 200

    resp = await client.get("/api/portfolio")
    data = resp.json()
    assert data["cash_balance"] < 10000.0
    tickers = [p["ticker"] for p in data["positions"]]
    assert "AAPL" in tickers


async def test_static_index(client):
    """GET / serves the placeholder index.html."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "FinAlly" in resp.text


async def test_static_spa_fallback(client):
    """Unknown paths return index.html (SPA fallback), not 404."""
    resp = await client.get("/some/unknown/path")
    assert resp.status_code == 200
    assert "FinAlly" in resp.text
