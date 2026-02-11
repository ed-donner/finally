"""HTTP-level tests for POST /api/chat endpoint."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.db import init_db
from app.llm import create_chat_router
from app.market.cache import PriceCache
from app.market.interface import MarketDataSource


class MockMarketDataSource(MarketDataSource):
    """Minimal mock for route tests."""

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
async def app(tmp_path, monkeypatch):
    """FastAPI app with chat router, isolated DB, and LLM_MOCK=true."""
    monkeypatch.setenv("LLM_MOCK", "true")

    db_path = str(tmp_path / "test.db")
    db = await init_db(db_path)

    price_cache = PriceCache()
    price_cache.update("AAPL", 150.00)
    price_cache.update("GOOGL", 175.00)
    price_cache.update("PYPL", 80.00)

    mock_market = MockMarketDataSource()

    application = FastAPI()
    application.include_router(create_chat_router(db, price_cache, mock_market))

    yield application, db
    await db.close()


@pytest.fixture
async def client(app):
    """Async HTTP client for testing chat endpoint."""
    application, _ = app
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_post_chat_default_message(client):
    """POST /api/chat with generic message returns 200 with structured response."""
    resp = await client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["message"], str)
    assert len(data["message"]) > 0
    assert data["trades"] == []
    assert data["watchlist_changes"] == []


async def test_post_chat_buy_executes_trade(client):
    """POST /api/chat with 'buy' keyword executes a trade."""
    resp = await client.post("/api/chat", json={"message": "buy some AAPL"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"]
    assert len(data["trades"]) == 1
    trade = data["trades"][0]
    assert trade["status"] == "executed"
    assert trade["ticker"] == "AAPL"
    assert trade["side"] == "buy"


async def test_post_chat_buy_updates_portfolio(app, client):
    """After buy via chat, cash decreases in the database."""
    _, db = app
    resp = await client.post("/api/chat", json={"message": "buy some stock"})
    assert resp.status_code == 200

    rows = await db.execute_fetchall(
        "SELECT cash_balance FROM users_profile WHERE id = 'default'"
    )
    assert rows[0][0] < 10000.0


async def test_post_chat_sell_failure_reported(client):
    """Sell without position returns 200 with status=failed (not HTTP error)."""
    resp = await client.post("/api/chat", json={"message": "sell AAPL"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["trades"]) == 1
    trade = data["trades"][0]
    assert trade["status"] == "failed"
    assert "Insufficient" in trade["error"]


async def test_post_chat_watchlist_add(client):
    """'add to watchlist' message adds ticker and returns applied result."""
    resp = await client.post(
        "/api/chat", json={"message": "add to watchlist"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["watchlist_changes"]) == 1
    change = data["watchlist_changes"][0]
    assert change["status"] == "applied"
    assert change["ticker"] == "PYPL"
    assert change["action"] == "add"


async def test_post_chat_empty_message_rejected(client):
    """POST /api/chat with empty message returns 422."""
    resp = await client.post("/api/chat", json={"message": ""})
    assert resp.status_code == 422


async def test_post_chat_missing_message_rejected(client):
    """POST /api/chat with missing message field returns 422."""
    resp = await client.post("/api/chat", json={})
    assert resp.status_code == 422


async def test_post_chat_messages_persisted(app, client):
    """After chat, both user and assistant messages are in the database."""
    _, db = app
    resp = await client.post("/api/chat", json={"message": "hello there"})
    assert resp.status_code == 200

    rows = await db.execute_fetchall(
        "SELECT role, content FROM chat_messages "
        "WHERE user_id = 'default' ORDER BY created_at"
    )
    assert len(rows) == 2
    assert rows[0][0] == "user"
    assert rows[0][1] == "hello there"
    assert rows[1][0] == "assistant"
    assert len(rows[1][1]) > 0
