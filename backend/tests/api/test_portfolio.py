"""Tests for portfolio endpoints."""


async def test_get_portfolio_initial(app_client):
    """Fresh portfolio has $10k cash and no positions."""
    resp = await app_client.get("/api/portfolio")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cash_balance"] == 10000.0
    assert data["total_value"] == 10000.0
    assert data["positions"] == []


async def test_buy_trade(app_client):
    """Buy shares, cash decreases, position appears."""
    resp = await app_client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 10, "side": "buy"},
    )
    assert resp.status_code == 200
    data = resp.json()

    trade = data["trade"]
    assert trade["ticker"] == "AAPL"
    assert trade["side"] == "buy"
    assert trade["quantity"] == 10
    assert trade["price"] == 190.0

    portfolio = data["portfolio"]
    assert portfolio["cash_balance"] == 10000.0 - (190.0 * 10)
    assert len(portfolio["positions"]) == 1
    assert portfolio["positions"][0]["ticker"] == "AAPL"
    assert portfolio["positions"][0]["quantity"] == 10


async def test_sell_trade(app_client):
    """Buy then sell shares."""
    await app_client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 10, "side": "buy"},
    )
    resp = await app_client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 5, "side": "sell"},
    )
    assert resp.status_code == 200
    portfolio = resp.json()["portfolio"]
    assert portfolio["positions"][0]["quantity"] == 5


async def test_sell_all_removes_position(app_client):
    """Selling all shares removes the position."""
    await app_client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 10, "side": "buy"},
    )
    resp = await app_client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 10, "side": "sell"},
    )
    assert resp.status_code == 200
    portfolio = resp.json()["portfolio"]
    assert len(portfolio["positions"]) == 0


async def test_insufficient_cash(app_client):
    """Cannot buy more than cash allows."""
    resp = await app_client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 1000, "side": "buy"},
    )
    assert resp.status_code == 400
    assert "Insufficient cash" in resp.json()["detail"]


async def test_insufficient_shares(app_client):
    """Cannot sell shares not owned."""
    resp = await app_client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 5, "side": "sell"},
    )
    assert resp.status_code == 400
    assert "Insufficient shares" in resp.json()["detail"]


async def test_no_price_available(app_client):
    """Cannot trade a ticker with no price data."""
    resp = await app_client.post(
        "/api/portfolio/trade",
        json={"ticker": "ZZZZ", "quantity": 1, "side": "buy"},
    )
    assert resp.status_code == 400
    assert "No price available" in resp.json()["detail"]


async def test_buy_accumulates_avg_cost(app_client):
    """Buying more shares updates the average cost."""
    await app_client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 10, "side": "buy"},
    )
    # Simulate a price change in the cache
    app_client._transport.app.state.price_cache.update("AAPL", 200.0)

    resp = await app_client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 10, "side": "buy"},
    )
    portfolio = resp.json()["portfolio"]
    pos = portfolio["positions"][0]
    assert pos["quantity"] == 20
    # Avg cost = (190*10 + 200*10) / 20 = 195
    assert pos["avg_cost"] == 195.0


async def test_portfolio_history(app_client):
    """History endpoint returns snapshots after a trade."""
    await app_client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 10, "side": "buy"},
    )
    resp = await app_client.get("/api/portfolio/history")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


async def test_invalid_side(app_client):
    """Invalid trade side is rejected."""
    resp = await app_client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 1, "side": "short"},
    )
    assert resp.status_code == 422


async def test_invalid_ticker(app_client):
    """Invalid ticker format is rejected."""
    resp = await app_client.post(
        "/api/portfolio/trade",
        json={"ticker": "aapl", "quantity": 1, "side": "buy"},
    )
    assert resp.status_code == 422
