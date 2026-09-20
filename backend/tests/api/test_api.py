"""End-to-end API tests: FastAPI app + real agents + in-memory SQLite + simulator prices."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


# Each test gets a fresh app: in-memory DB, mock LLM, simulator prices (no network, no API keys).
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LLM_MOCK", "true")
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    with TestClient(create_app(":memory:", snapshot_interval=3600)) as c:
        yield c


@pytest.fixture
def db(client):
    return client.app.state.agents.db


# Helper for POST /api/trade; sells via side="sell" (named `buy` because that is the common case).
def buy(client, ticker="AAPL", qty=1, side="buy"):
    return client.post("/api/trade", json={"ticker": ticker, "side": side, "quantity": qty})


def test_fresh_state(client):
    p = client.get("/api/portfolio").json()
    assert p["cash_balance"] == 10000 and p["positions"] == [] and p["total_value"] == 10000
    assert len(client.get("/api/watchlist").json()) == 10
    assert client.get("/api/health").json() == {"status": "ok"}
    assert len(client.get("/api/portfolio/history").json()) >= 1


def test_buy_then_sell_updates_cash_and_positions(client):
    r = buy(client, "AAPL", 5)
    assert r.status_code == 200
    price = r.json()["trade"]["price"]
    p = client.get("/api/portfolio").json()
    assert p["cash_balance"] == pytest.approx(10000 - 5 * price)
    assert p["positions"][0]["ticker"] == "AAPL" and p["positions"][0]["quantity"] == 5

    assert buy(client, "AAPL", 5, "sell").status_code == 200
    p = client.get("/api/portfolio").json()
    assert p["positions"] == []
    assert p["cash_balance"] == pytest.approx(10000, abs=1e-6)


def test_average_cost_on_second_buy(client):
    buy(client, "MSFT", 2)
    buy(client, "MSFT", 2)
    pos = client.get("/api/portfolio").json()["positions"][0]
    assert pos["quantity"] == 4 and pos["avg_cost"] > 0


def test_rejections(client):
    # Each rejection must leave the portfolio untouched (checked at the end).
    assert buy(client, "AAPL", 100000).status_code == 400  # insufficient cash
    assert "insufficient cash" in buy(client, "AAPL", 100000).json()["error"]
    assert buy(client, "AAPL", 1, "sell").status_code == 400  # nothing to sell
    assert buy(client, "AAPL", -1).status_code == 400
    assert buy(client, "AAPL", 0).status_code == 400
    assert buy(client, "not a ticker", 1).status_code == 400
    assert client.get("/api/portfolio").json()["cash_balance"] == 10000


def test_position_limit(client):
    # ~$9k of a ~$190 stock is >50% of the portfolio -> rejected by the risk agent
    r = buy(client, "AAPL", 40)
    assert r.status_code == 400 and "portfolio" in r.json()["error"]


def test_trade_of_unwatched_ticker_gets_price(client):
    assert buy(client, "PYPL", 1).status_code == 200


def test_watchlist_add_remove(client):
    r = client.post("/api/watchlist", json={"ticker": "pypl", "action": "add"})
    assert r.status_code == 200 and "PYPL" in [e["ticker"] for e in r.json()["watchlist"]]
    r = client.post("/api/watchlist", json={"ticker": "PYPL", "action": "remove"})
    assert "PYPL" not in [e["ticker"] for e in r.json()["watchlist"]]
    assert client.post("/api/watchlist", json={"ticker": "PYPL", "action": "remove"}).status_code == 400
    assert client.post("/api/watchlist", json={"ticker": "!!", "action": "add"}).status_code == 400
    # Bad action is a validation error (422), unlike a domain failure (400).
    assert client.post("/api/watchlist", json={"ticker": "A", "action": "nuke"}).status_code == 422
    assert client.delete("/api/watchlist/NFLX").status_code == 200


def test_analysis_and_risk(client):
    assert buy(client, "NVDA", 2).status_code == 200
    a = client.get("/api/analysis").json()
    assert a["positions"][0]["ticker"] == "NVDA" and a["cash_pct"] < 100
    r = client.get("/api/risk-assessment").json()
    assert r["level"] in ("low", "moderate", "high") and r["largest_position"]["ticker"] == "NVDA"


def test_chat_executes_trades_and_watchlist(client):
    r = client.post("/api/chat", json={"message": "buy 3 AAPL and add PYPL to my watchlist"})
    assert r.status_code == 200
    body = r.json()
    assert body["executed_trades"][0]["ok"] and body["watchlist_changes"][0]["ok"]
    assert client.get("/api/portfolio").json()["positions"][0]["ticker"] == "AAPL"
    assert "PYPL" in [e["ticker"] for e in client.get("/api/watchlist").json()]


def test_chat_failed_trade_is_reported(client):
    body = client.post("/api/chat", json={"message": "sell 5 TSLA"}).json()
    assert body["executed_trades"][0]["ok"] is False
    assert "Could not sell" in body["message"]


def test_chat_batch_is_validated_cumulatively(client):
    # ~$3.8k (38%) passes the 50% position limit; the second identical buy would reach ~76%
    body = client.post("/api/chat", json={"message": "buy 20 AAPL then buy 20 AAPL"}).json()
    assert [t["ok"] for t in body["executed_trades"]] == [True, False]
    assert client.get("/api/portfolio").json()["positions"][0]["quantity"] == 20


def test_chat_validation_and_history(client, db):
    assert client.post("/api/chat", json={"message": ""}).status_code == 422
    client.post("/api/chat", json={"message": "hello"})
    rows = db.query("SELECT * FROM chat_messages")
    assert len(rows) == 1 and rows[0]["user_message"] == "hello"


def test_audit_log_shares_correlation_id(client, db):
    client.post("/api/chat", json={"message": "buy 1 AAPL"})
    # One chat turn should leave audit rows from all four participating agents, all under one
    # correlation id that the trade row also carries.
    logs = db.query(
        "SELECT agent_id, correlation_id FROM agent_logs ORDER BY created_at DESC"
    )
    agents = {r["agent_id"] for r in logs}
    assert {"orchestrator", "risk", "portfolio", "analyzer"} <= agents
    trade = db.query_one("SELECT * FROM trades")
    assert trade["source"] == "chat" and trade["agent_id"] == "portfolio"
    assert trade["correlation_id"] in {r["correlation_id"] for r in logs}


def test_manual_source_cannot_be_spoofed(client, db):
    # The client claims source='system'; the API must still record 'manual'.
    client.post("/api/trade", json={"ticker": "AAPL", "side": "buy", "quantity": 1, "source": "system"})
    assert db.query_one("SELECT source FROM trades")["source"] == "manual"
