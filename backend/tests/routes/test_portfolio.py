"""Tests for portfolio endpoints."""


from app.db import upsert_position


class TestGetPortfolio:
    async def test_empty_portfolio(self, client):
        resp = await client.get("/api/portfolio")
        assert resp.status_code == 200
        data = resp.json()
        assert data["positions"] == []
        assert data["cash"] == 10000.0
        assert data["total_value"] == 10000.0
        assert data["unrealized_pnl"] == 0.0

    async def test_portfolio_with_positions(self, client, test_db):
        await upsert_position("AAPL", 10, 180.0)
        resp = await client.get("/api/portfolio")
        data = resp.json()
        assert len(data["positions"]) == 1

        pos = data["positions"][0]
        assert pos["ticker"] == "AAPL"
        assert pos["quantity"] == 10
        assert pos["avg_cost"] == 180.0
        assert pos["current_price"] == 190.50  # from price_cache fixture
        assert pos["market_value"] == 1905.0
        assert pos["unrealized_pnl"] == 105.0


class TestExecuteTrade:
    async def test_buy(self, client):
        resp = await client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "side": "buy", "quantity": 5},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["trade"]["ticker"] == "AAPL"
        assert data["trade"]["side"] == "buy"
        assert data["trade"]["quantity"] == 5
        assert data["trade"]["price"] == 190.50
        # Cash should be 10000 - (5 * 190.50) = 9047.50
        assert data["cash"] == 9047.5

    async def test_sell(self, client, test_db):
        # First buy some shares
        await upsert_position("AAPL", 10, 180.0)
        resp = await client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "side": "sell", "quantity": 5},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["trade"]["side"] == "sell"
        # Cash should be 10000 + (5 * 190.50) = 10952.50
        assert data["cash"] == 10952.5

    async def test_sell_all_shares_removes_position(self, client, test_db):
        await upsert_position("AAPL", 5, 180.0)
        resp = await client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "side": "sell", "quantity": 5},
        )
        assert resp.status_code == 200
        # Check position is gone
        portfolio = await client.get("/api/portfolio")
        assert portfolio.json()["positions"] == []

    async def test_buy_insufficient_cash(self, client):
        resp = await client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "side": "buy", "quantity": 1000},
        )
        assert resp.status_code == 400
        assert "Insufficient cash" in resp.json()["detail"]

    async def test_sell_insufficient_shares(self, client):
        resp = await client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "side": "sell", "quantity": 5},
        )
        assert resp.status_code == 400
        assert "Insufficient shares" in resp.json()["detail"]

    async def test_invalid_side(self, client):
        resp = await client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "side": "short", "quantity": 5},
        )
        assert resp.status_code == 400
        assert "side must be" in resp.json()["detail"]

    async def test_zero_quantity(self, client):
        resp = await client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "side": "buy", "quantity": 0},
        )
        assert resp.status_code == 400
        assert "quantity must be positive" in resp.json()["detail"]

    async def test_no_price_available(self, client):
        resp = await client.post(
            "/api/portfolio/trade",
            json={"ticker": "UNKNOWN", "side": "buy", "quantity": 1},
        )
        assert resp.status_code == 400
        assert "No price available" in resp.json()["detail"]

    async def test_buy_updates_avg_cost(self, client, test_db):
        # Buy 10 at 190.50
        await client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "side": "buy", "quantity": 10},
        )
        # Buy 10 more at 190.50 (same price since cache is static)
        await client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "side": "buy", "quantity": 10},
        )
        portfolio = await client.get("/api/portfolio")
        pos = portfolio.json()["positions"][0]
        assert pos["quantity"] == 20
        assert pos["avg_cost"] == 190.50


class TestPortfolioHistory:
    async def test_history_empty(self, client):
        resp = await client.get("/api/portfolio/history")
        assert resp.status_code == 200
        assert resp.json()["snapshots"] == []

    async def test_history_after_trade(self, client):
        # Execute a trade to trigger snapshot
        await client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "side": "buy", "quantity": 1},
        )
        resp = await client.get("/api/portfolio/history")
        snapshots = resp.json()["snapshots"]
        assert len(snapshots) == 1
        assert snapshots[0]["total_value"] > 0
