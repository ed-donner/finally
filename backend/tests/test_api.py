"""API integration tests for the FinAlly backend app."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from app import llm as llm_module
from app.db import LEGACY_DEFAULT_WATCHLIST
from app.main import create_app
from app.market import PriceCache
from app.market.stream import _generate_events


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "finally.db"
    monkeypatch.setenv("FINALLY_DB_PATH", str(db_file))
    monkeypatch.setenv("LLM_MOCK", "true")
    monkeypatch.setenv("SNAPSHOT_INTERVAL_SECONDS", "0.2")
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def test_health(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["market_source"] == "SimulatorDataSource"


def test_default_watchlist_seeded(client: TestClient):
    response = client.get("/api/watchlist")
    assert response.status_code == 200

    items = response.json()["items"]
    assert len(items) == 60
    assert {item["ticker"] for item in items} >= {"AAPL", "MSFT", "INTC", "JPM", "XOM"}
    assert all("group" in item for item in items)
    assert {item["group"]["key"] for item in items} >= {
        "tech",
        "financials",
        "healthcare",
        "consumer",
        "industrials-energy",
    }


def test_watchlist_crud(client: TestClient):
    create_resp = client.post("/api/watchlist", json={"ticker": "aapl"})
    assert create_resp.status_code == 409

    duplicate_resp = client.post("/api/watchlist", json={"ticker": "AAPL"})
    assert duplicate_resp.status_code == 409

    create_new_resp = client.post("/api/watchlist", json={"ticker": "shop"})
    assert create_new_resp.status_code == 201
    assert create_new_resp.json()["ticker"] == "SHOP"

    delete_resp = client.delete("/api/watchlist/SHOP")
    assert delete_resp.status_code == 204


def test_watchlist_new_additions_use_custom_group(client: TestClient):
    response = client.post("/api/watchlist", json={"ticker": "shop"})
    assert response.status_code == 201

    watchlist = client.get("/api/watchlist").json()["items"]
    shop = next(item for item in watchlist if item["ticker"] == "SHOP")
    assert shop["group"]["key"] == "custom"
    assert shop["group"]["label"] == "Custom"


def test_legacy_default_watchlist_is_migrated_if_unchanged(tmp_path, monkeypatch):
    db_file = tmp_path / "finally.db"
    monkeypatch.setenv("FINALLY_DB_PATH", str(db_file))
    monkeypatch.setenv("LLM_MOCK", "true")
    monkeypatch.setenv("SNAPSHOT_INTERVAL_SECONDS", "0.2")
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)

    import sqlite3
    from uuid import uuid4

    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        CREATE TABLE users_profile (
            id TEXT PRIMARY KEY,
            cash_balance REAL NOT NULL DEFAULT 10000.0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE watchlist (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            added_at TEXT NOT NULL,
            UNIQUE(user_id, ticker)
        )
        """
    )
    conn.execute(
        "INSERT INTO users_profile (id, cash_balance, created_at) VALUES ('default', 10000.0, '2025-01-01T00:00:00+00:00')"
    )
    for ticker in LEGACY_DEFAULT_WATCHLIST:
        conn.execute(
            "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, 'default', ?, '2025-01-01T00:00:00+00:00')",
            (str(uuid4()), ticker),
        )
    conn.commit()
    conn.close()

    app = create_app()
    with TestClient(app) as local_client:
        payload = local_client.get("/api/watchlist").json()
        assert len(payload["items"]) == 60
        assert "XOM" in {item["ticker"] for item in payload["items"]}


def test_legacy_ungrouped_watchlist_is_migrated_even_if_user_changed_it(tmp_path, monkeypatch):
    db_file = tmp_path / "finally.db"
    monkeypatch.setenv("FINALLY_DB_PATH", str(db_file))
    monkeypatch.setenv("LLM_MOCK", "true")
    monkeypatch.setenv("SNAPSHOT_INTERVAL_SECONDS", "0.2")
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)

    import sqlite3
    from uuid import uuid4

    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        CREATE TABLE users_profile (
            id TEXT PRIMARY KEY,
            cash_balance REAL NOT NULL DEFAULT 10000.0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE watchlist (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            added_at TEXT NOT NULL,
            UNIQUE(user_id, ticker)
        )
        """
    )
    conn.execute(
        "INSERT INTO users_profile (id, cash_balance, created_at) VALUES ('default', 10000.0, '2025-01-01T00:00:00+00:00')"
    )
    changed = [ticker for ticker in LEGACY_DEFAULT_WATCHLIST if ticker != "NFLX"] + ["IBM"]
    for ticker in changed:
        conn.execute(
            "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, 'default', ?, '2025-01-01T00:00:00+00:00')",
            (str(uuid4()), ticker),
        )
    conn.commit()
    conn.close()

    app = create_app()
    with TestClient(app) as local_client:
        payload = local_client.get("/api/watchlist").json()
        tickers = {item["ticker"] for item in payload["items"]}
        assert len(tickers) == 60
        assert "IBM" not in tickers
        assert "XOM" in tickers


def test_portfolio_trade_and_history(client: TestClient):
    trade_resp = client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 2, "side": "buy"},
    )
    assert trade_resp.status_code == 200
    trade_data = trade_resp.json()
    assert trade_data["trade"]["ticker"] == "AAPL"
    assert trade_data["trade"]["side"] == "buy"

    portfolio_resp = client.get("/api/portfolio")
    assert portfolio_resp.status_code == 200
    portfolio = portfolio_resp.json()
    assert portfolio["cash_balance"] < 10000.0
    assert any(pos["ticker"] == "AAPL" for pos in portfolio["positions"])

    history_resp = client.get("/api/portfolio/history")
    assert history_resp.status_code == 200
    assert len(history_resp.json()["items"]) >= 1


def test_chat_mock_auto_executes_actions(client: TestClient):
    chat_resp = client.post(
        "/api/chat", json={"message": "buy 1 AAPL and add SHOP to watchlist"}
    )
    assert chat_resp.status_code == 200

    payload = chat_resp.json()
    assert payload["actions"]["trades"]
    assert payload["actions"]["trades"][0]["ticker"] == "AAPL"
    assert {item["ticker"] for item in payload["actions"]["watchlist_changes"]} >= {"SHOP"}

    watchlist_resp = client.get("/api/watchlist")
    tickers = {item["ticker"] for item in watchlist_resp.json()["items"]}
    assert "SHOP" in tickers


@pytest.mark.asyncio
async def test_sse_event_generator_emits_retry_and_data():
    cache = PriceCache()
    cache.update("AAPL", 190.0)

    class DummyClient:
        host = "test-client"

    class DummyRequest:
        client = DummyClient()

        def __init__(self) -> None:
            self._calls = 0

        async def is_disconnected(self) -> bool:
            self._calls += 1
            return self._calls > 1

    stream = _generate_events(cache, DummyRequest(), interval=0.01)
    first = await anext(stream)
    second = await anext(stream)

    assert first.startswith("retry: 1000")
    assert second.startswith("data: ")


def test_chat_handles_openrouter_http_error(tmp_path, monkeypatch):
    db_file = tmp_path / "finally.db"
    monkeypatch.setenv("FINALLY_DB_PATH", str(db_file))
    monkeypatch.setenv("LLM_MOCK", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "bad-key")
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)

    def fake_completion(**kwargs):  # noqa: ANN003
        raise RuntimeError("simulated litellm failure")

    monkeypatch.setattr(llm_module, "completion", fake_completion)

    app = create_app()
    with TestClient(app) as local_client:
        response = local_client.post("/api/chat", json={"message": "test"})
        assert response.status_code == 200
        payload = response.json()
        assert "LLM request failed" in payload["message"]
        assert payload["actions"]["trades"] == []
