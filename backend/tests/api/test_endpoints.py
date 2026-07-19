"""API endpoint tests — response shapes and behavior per TEAM_CONTRACT §3."""


def test_health(client):
    c, _ = client
    r = c.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_portfolio_initial(client):
    c, _ = client
    r = c.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["cash_balance"] == 10000.0
    assert body["total_value"] == 10000.0
    assert body["positions"] == []
    for key in ("positions_value", "total_unrealized_pl"):
        assert key in body


def test_watchlist_lists_defaults_with_prices(client):
    c, _ = client
    r = c.get("/api/watchlist")
    assert r.status_code == 200
    wl = r.json()["watchlist"]
    assert len(wl) == 10
    assert wl[0]["ticker"] == "AAPL"
    assert wl[0]["price"] == 100.0
    assert wl[0]["direction"] in ("up", "down", "flat")


def test_buy_then_portfolio_reflects_position(client):
    c, _ = client
    r = c.post("/api/portfolio/trade", json={"ticker": "AAPL", "quantity": 5, "side": "buy"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["trade"]["ticker"] == "AAPL"
    pf = body["portfolio"]
    assert pf["cash_balance"] == 9500.0
    assert pf["positions"][0]["ticker"] == "AAPL"
    assert pf["positions"][0]["quantity"] == 5


def test_buy_insufficient_cash_returns_400(client):
    c, _ = client
    r = c.post("/api/portfolio/trade", json={"ticker": "AAPL", "quantity": 1000, "side": "buy"})
    assert r.status_code == 400
    assert "Insufficient cash" in r.json()["detail"]


def test_sell_without_position_returns_400(client):
    c, _ = client
    r = c.post("/api/portfolio/trade", json={"ticker": "AAPL", "quantity": 1, "side": "sell"})
    assert r.status_code == 400
    assert "Insufficient shares" in r.json()["detail"]


def test_trade_unknown_ticker_no_price_400(client):
    c, _ = client
    r = c.post("/api/portfolio/trade", json={"ticker": "ZZZZ", "quantity": 1, "side": "buy"})
    assert r.status_code == 400
    assert "No price" in r.json()["detail"]


def test_add_and_remove_watchlist(client):
    c, _ = client
    r = c.post("/api/watchlist", json={"ticker": "PYPL"})
    assert r.status_code == 200
    tickers = [w["ticker"] for w in r.json()["watchlist"]]
    assert "PYPL" in tickers

    r = c.post("/api/watchlist", json={"ticker": "PYPL"})  # duplicate
    assert r.status_code == 400

    r = c.delete("/api/watchlist/PYPL")
    assert r.status_code == 200
    tickers = [w["ticker"] for w in r.json()["watchlist"]]
    assert "PYPL" not in tickers

    r = c.delete("/api/watchlist/PYPL")  # already gone
    assert r.status_code == 400


def test_history_has_snapshot_after_trade(client):
    c, _ = client
    c.post("/api/portfolio/trade", json={"ticker": "AAPL", "quantity": 1, "side": "buy"})
    r = c.get("/api/portfolio/history")
    assert r.status_code == 200
    snaps = r.json()["snapshots"]
    assert len(snaps) >= 1
    assert "total_value" in snaps[0] and "recorded_at" in snaps[0]


def test_chat_mock_executes_buy(client):
    c, _ = client
    r = c.post("/api/chat", json={"message": "buy 5 AAPL please"})
    assert r.status_code == 200
    body = r.json()
    assert body["trades"][0]["ticker"] == "AAPL"
    assert body["trades"][0]["status"] == "executed"
    assert body["portfolio"]["cash_balance"] == 9500.0


def test_chat_mock_add_watchlist(client):
    c, _ = client
    r = c.post("/api/chat", json={"message": "add PYPL"})
    assert r.status_code == 200
    body = r.json()
    assert body["watchlist_changes"][0]["ticker"] == "PYPL"
    assert body["watchlist_changes"][0]["status"] == "applied"


def test_chat_mock_analytical_no_actions(client):
    c, _ = client
    r = c.post("/api/chat", json={"message": "how is my portfolio doing?"})
    assert r.status_code == 200
    body = r.json()
    assert body["trades"] == []
    assert body["watchlist_changes"] == []
    assert body["message"]
