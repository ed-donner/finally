"""Tests for /api/watchlist endpoints."""

from __future__ import annotations

ITEM_KEYS = {
    "ticker", "price", "previous_price", "change_percent",
    "day_change_percent", "direction", "timestamp",
}


def test_get_watchlist(client):
    r = client.get("/api/watchlist")
    assert r.status_code == 200
    tickers = r.json()["tickers"]
    assert len(tickers) == 10
    assert all(set(t) == ITEM_KEYS for t in tickers)
    aapl = next(t for t in tickers if t["ticker"] == "AAPL")
    assert aapl["price"] == 100.0


def test_add_ticker(client):
    r = client.post("/api/watchlist", json={"ticker": "pypl"})
    assert r.status_code == 201
    body = r.json()
    assert body["ticker"] == "PYPL"
    assert body["price"] == 60.0
    assert set(body) == ITEM_KEYS
    assert "PYPL" in client.source.get_tickers()
    assert "PYPL" in [t["ticker"] for t in client.get("/api/watchlist").json()["tickers"]]


def test_add_duplicate_409(client):
    r = client.post("/api/watchlist", json={"ticker": "AAPL"})
    assert r.status_code == 409


def test_add_invalid_400(client):
    assert client.post("/api/watchlist", json={"ticker": "not a ticker"}).status_code == 400
    assert client.post("/api/watchlist", json={}).status_code == 400


def test_remove_ticker(client):
    r = client.delete("/api/watchlist/msft")
    assert r.status_code == 200
    assert r.json() == {"ticker": "MSFT", "removed": True}
    assert "MSFT" not in client.source.get_tickers()


def test_remove_missing_404(client):
    r = client.delete("/api/watchlist/PYPL")
    assert r.status_code == 404
