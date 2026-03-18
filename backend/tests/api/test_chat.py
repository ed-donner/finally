"""Tests for chat endpoint."""


async def test_chat_generic_message(app_client, monkeypatch):
    """Chat returns a response for a generic message."""
    monkeypatch.setenv("LLM_MOCK", "true")
    resp = await app_client.post("/api/chat", json={"message": "Hello"})
    assert resp.status_code == 200
    data = resp.json()
    msg = data["message"]
    assert "Mock:" in msg["content"]
    assert msg["actions"] is None


async def test_chat_buy_executes_trade(app_client, monkeypatch):
    """Chat with 'buy' triggers auto-execution of a trade."""
    monkeypatch.setenv("LLM_MOCK", "true")
    resp = await app_client.post("/api/chat", json={"message": "buy AAPL"})
    assert resp.status_code == 200
    data = resp.json()
    msg = data["message"]
    assert msg["actions"] is not None
    assert len(msg["actions"]["trades"]) == 1
    trade = msg["actions"]["trades"][0]
    assert trade["ticker"] == "AAPL"
    assert trade["side"] == "buy"
    assert trade["quantity"] == 10

    # Verify portfolio was updated
    portfolio = await app_client.get("/api/portfolio")
    p = portfolio.json()
    assert p["cash_balance"] < 10000.0
    assert len(p["positions"]) == 1


async def test_chat_sell_without_position_errors(app_client, monkeypatch):
    """Selling without owning shares results in a trade error."""
    monkeypatch.setenv("LLM_MOCK", "true")
    resp = await app_client.post("/api/chat", json={"message": "sell AAPL"})
    assert resp.status_code == 200
    data = resp.json()
    msg = data["message"]
    assert msg["actions"] is not None
    assert len(msg["actions"]["errors"]) > 0
    assert len(msg["actions"]["trades"]) == 0


async def test_chat_add_watchlist(app_client, monkeypatch):
    """Chat with 'add' triggers watchlist addition."""
    monkeypatch.setenv("LLM_MOCK", "true")
    resp = await app_client.post("/api/chat", json={"message": "add PYPL"})
    assert resp.status_code == 200
    data = resp.json()
    msg = data["message"]
    assert msg["actions"] is not None
    assert len(msg["actions"]["watchlist_changes"]) == 1
    assert msg["actions"]["watchlist_changes"][0]["ticker"] == "PYPL"
    assert msg["actions"]["watchlist_changes"][0]["action"] == "added"


async def test_chat_stores_messages(app_client, monkeypatch):
    """Chat stores both user and assistant messages."""
    monkeypatch.setenv("LLM_MOCK", "true")
    resp = await app_client.post("/api/chat", json={"message": "Hello there"})
    assert resp.status_code == 200
    data = resp.json()
    msg = data["message"]
    assert "id" in msg
    assert "created_at" in msg
    assert msg["role"] == "assistant"
