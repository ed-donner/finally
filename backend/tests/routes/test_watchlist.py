"""Tests for watchlist endpoints."""



class TestGetWatchlist:
    async def test_default_watchlist(self, client):
        resp = await client.get("/api/watchlist")
        assert resp.status_code == 200
        data = resp.json()
        tickers = [item["ticker"] for item in data["watchlist"]]
        assert "AAPL" in tickers
        assert "GOOGL" in tickers
        assert len(tickers) == 10

    async def test_watchlist_includes_prices(self, client):
        resp = await client.get("/api/watchlist")
        data = resp.json()
        aapl = next(item for item in data["watchlist"] if item["ticker"] == "AAPL")
        assert aapl["price"] == 190.50


class TestAddTicker:
    async def test_add_new_ticker(self, client, price_cache):
        price_cache.update("PYPL", 65.00)
        resp = await client.post("/api/watchlist", json={"ticker": "PYPL"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ticker"] == "PYPL"
        assert "added_at" in data

    async def test_add_duplicate_ticker(self, client):
        resp = await client.post("/api/watchlist", json={"ticker": "AAPL"})
        assert resp.status_code == 409


class TestRemoveTicker:
    async def test_remove_existing_ticker(self, client):
        resp = await client.delete("/api/watchlist/AAPL")
        assert resp.status_code == 200
        assert resp.json()["removed"] is True

    async def test_remove_nonexistent_ticker(self, client):
        resp = await client.delete("/api/watchlist/DOESNOTEXIST")
        assert resp.status_code == 404
