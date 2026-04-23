"""HTTP route tests covering health, watchlist, portfolio, trade, and history."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import db

# ---- health ----

def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---- watchlist ----

def test_watchlist_default_seed(client: TestClient) -> None:
    r = client.get("/api/watchlist")
    assert r.status_code == 200
    body = r.json()
    tickers = [e["ticker"] for e in body]
    assert tickers == db.DEFAULT_WATCHLIST


def test_watchlist_add(client: TestClient) -> None:
    r = client.post("/api/watchlist", json={"ticker": "PYPL"})
    assert r.status_code == 201
    assert r.json()["ticker"] == "PYPL"
    assert "PYPL" in db.watchlist.list_tickers()


def test_watchlist_add_duplicate_409(client: TestClient) -> None:
    r = client.post("/api/watchlist", json={"ticker": "AAPL"})
    assert r.status_code == 409


def test_watchlist_add_invalid_ticker(client: TestClient) -> None:
    r = client.post("/api/watchlist", json={"ticker": "B@D"})
    assert r.status_code == 400


def test_watchlist_remove(client: TestClient) -> None:
    r = client.delete("/api/watchlist/AAPL")
    assert r.status_code == 204
    assert "AAPL" not in db.watchlist.list_tickers()


def test_watchlist_remove_missing_404(client: TestClient) -> None:
    r = client.delete("/api/watchlist/NOPE")
    assert r.status_code == 404


# ---- portfolio ----

def test_portfolio_initial_state(client: TestClient) -> None:
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["cash_balance"] == 10_000.0
    assert body["positions"] == []
    assert body["total_value"] == 10_000.0


def test_trade_buy_then_portfolio_reflects_position(client: TestClient) -> None:
    buy = client.post("/api/portfolio/trade", json={
        "ticker": "AAPL", "side": "buy", "quantity": 1,
    })
    assert buy.status_code == 200, buy.text
    body = buy.json()
    assert body["ticker"] == "AAPL"
    assert body["side"] == "buy"
    assert body["quantity"] == 1
    assert body["price"] > 0

    portfolio = client.get("/api/portfolio").json()
    assert len(portfolio["positions"]) == 1
    assert portfolio["positions"][0]["ticker"] == "AAPL"
    assert portfolio["cash_balance"] < 10_000.0


def test_trade_buy_insufficient_cash_400(client: TestClient) -> None:
    r = client.post("/api/portfolio/trade", json={
        "ticker": "AAPL", "side": "buy", "quantity": 10_000,
    })
    assert r.status_code == 400
    assert "insufficient" in r.json()["detail"].lower()


def test_trade_sell_without_position_400(client: TestClient) -> None:
    r = client.post("/api/portfolio/trade", json={
        "ticker": "AAPL", "side": "sell", "quantity": 1,
    })
    assert r.status_code == 400


def test_trade_invalid_side_422(client: TestClient) -> None:
    # Pydantic literal validation runs before route handler — 422
    r = client.post("/api/portfolio/trade", json={
        "ticker": "AAPL", "side": "hodl", "quantity": 1,
    })
    assert r.status_code == 422


def test_trade_zero_quantity_422(client: TestClient) -> None:
    r = client.post("/api/portfolio/trade", json={
        "ticker": "AAPL", "side": "buy", "quantity": 0,
    })
    assert r.status_code == 422


def test_portfolio_history_seeded_immediately(client: TestClient) -> None:
    r = client.get("/api/portfolio/history")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["total_value"] == 10_000.0


def test_trade_records_snapshot_immediately(client: TestClient) -> None:
    before = client.get("/api/portfolio/history").json()
    buy = client.post("/api/portfolio/trade", json={
        "ticker": "AAPL", "side": "buy", "quantity": 1,
    })
    assert buy.status_code == 200, buy.text
    after = client.get("/api/portfolio/history").json()
    assert len(after) == len(before) + 1
