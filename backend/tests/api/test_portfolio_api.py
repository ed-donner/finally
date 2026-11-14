"""Tests for /api/portfolio endpoints."""

from __future__ import annotations

import pytest

PORTFOLIO_KEYS = {
    "cash_balance", "positions_value", "total_value",
    "unrealized_pnl", "unrealized_pnl_percent", "positions",
}


def trade(client, ticker, quantity, side):
    return client.post(
        "/api/portfolio/trade", json={"ticker": ticker, "quantity": quantity, "side": side}
    )


def test_initial_portfolio(client):
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == PORTFOLIO_KEYS
    assert body["cash_balance"] == 10000.0
    assert body["total_value"] == 10000.0
    assert body["positions"] == []


def test_buy(client):
    r = trade(client, "aapl", 10, "buy")
    assert r.status_code == 200
    body = r.json()
    assert set(body["trade"]) == {"id", "ticker", "side", "quantity", "price", "executed_at"}
    assert body["trade"]["ticker"] == "AAPL"
    assert body["trade"]["price"] == 100.0
    assert body["portfolio"]["cash_balance"] == 9000.0
    (pos,) = body["portfolio"]["positions"]
    assert pos["ticker"] == "AAPL" and pos["quantity"] == 10 and pos["weight"] == 0.1


def test_sell_all_removes_position(client):
    trade(client, "AAPL", 10, "buy")
    r = trade(client, "AAPL", 10, "sell")
    assert r.status_code == 200
    assert r.json()["portfolio"]["positions"] == []
    assert r.json()["portfolio"]["cash_balance"] == 10000.0


def test_insufficient_cash(client):
    r = trade(client, "NVDA", 100, "buy")
    assert r.status_code == 400
    assert "Insufficient cash" in r.json()["detail"]


def test_oversell(client):
    trade(client, "AAPL", 1, "buy")
    r = trade(client, "AAPL", 2, "sell")
    assert r.status_code == 400
    assert "Insufficient shares" in r.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        {"ticker": "AAPL", "quantity": 1, "side": "hold"},
        {"ticker": "AAPL", "quantity": 0, "side": "buy"},
        {"ticker": "AAPL", "quantity": -5, "side": "buy"},
        {"ticker": "AAPL", "quantity": "abc", "side": "buy"},
        {"ticker": "!!", "quantity": 1, "side": "buy"},
        {"ticker": "AAPL", "side": "buy"},
        {},
    ],
)
def test_invalid_trade_is_400(client, payload):
    r = client.post("/api/portfolio/trade", json=payload)
    assert r.status_code == 400
    assert isinstance(r.json()["detail"], str)


def test_unknown_ticker_no_price(client):
    r = trade(client, "ZZZZ", 1, "buy")
    assert r.status_code == 400
    assert "No price" in r.json()["detail"]


def test_trade_tracks_and_untracks_ticker(client):
    client.delete("/api/watchlist/AAPL")
    assert "AAPL" not in client.source.get_tickers()
    # Price still known from before removal? No: removal evicts it, so re-add a price.
    client.app.state.price_cache.update("AAPL", 100.0)
    assert trade(client, "AAPL", 1, "buy").status_code == 200
    assert "AAPL" in client.source.get_tickers()
    assert trade(client, "AAPL", 1, "sell").status_code == 200
    assert "AAPL" not in client.source.get_tickers()


def test_history(client):
    r = client.get("/api/portfolio/history")
    assert r.status_code == 200
    assert r.json() == {"snapshots": []}
    trade(client, "AAPL", 1, "buy")
    snaps = client.get("/api/portfolio/history").json()["snapshots"]
    assert len(snaps) == 1
    assert set(snaps[0]) == {"total_value", "recorded_at"}
    assert snaps[0]["total_value"] == 10000.0
