"""Tests for the watchlist router endpoints."""


async def test_get_watchlist_returns_seed_items(client):
    """GET /api/watchlist should return all 10 seed tickers."""
    resp = await client.get("/api/watchlist")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 10
    assert len(body["items"]) == 10


async def test_get_watchlist_enriches_with_prices(client):
    """AAPL should have price data from the seeded PriceCache."""
    resp = await client.get("/api/watchlist")
    body = resp.json()
    aapl = next(item for item in body["items"] if item["ticker"] == "AAPL")
    assert aapl["price"] == 190.50
    assert aapl["direction"] == "flat"


async def test_get_watchlist_missing_price_is_null(client):
    """Tickers not in PriceCache should have null price fields."""
    resp = await client.get("/api/watchlist")
    body = resp.json()
    tsla = next(item for item in body["items"] if item["ticker"] == "TSLA")
    assert tsla["price"] is None
    assert tsla["direction"] is None


async def test_add_ticker(client, mock_market_data_source):
    """POST /api/watchlist should add the ticker and notify the market data source."""
    resp = await client.post("/api/watchlist", json={"ticker": "PYPL"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["ticker"] == "PYPL"
    assert "PYPL" in mock_market_data_source.added


async def test_add_ticker_case_insensitive(client):
    """Ticker input should be normalized to uppercase."""
    resp = await client.post("/api/watchlist", json={"ticker": " pypl "})
    assert resp.status_code == 201
    assert resp.json()["ticker"] == "PYPL"


async def test_add_duplicate_returns_409(client):
    """Adding a ticker already in the watchlist should return 409."""
    resp = await client.post("/api/watchlist", json={"ticker": "AAPL"})
    assert resp.status_code == 409


async def test_remove_ticker(client, mock_market_data_source):
    """DELETE /api/watchlist/AAPL should remove it and notify the market data source."""
    resp = await client.delete("/api/watchlist/AAPL")
    assert resp.status_code == 200
    body = resp.json()
    assert body["removed"] == "AAPL"
    assert "AAPL" in mock_market_data_source.removed


async def test_remove_nonexistent_returns_404(client):
    """Deleting a ticker not in the watchlist should return 404."""
    resp = await client.delete("/api/watchlist/FAKE")
    assert resp.status_code == 404


async def test_add_then_get_shows_new_ticker(client):
    """After adding PYPL, GET should show 11 tickers including PYPL."""
    await client.post("/api/watchlist", json={"ticker": "PYPL"})
    resp = await client.get("/api/watchlist")
    body = resp.json()
    assert body["count"] == 11
    tickers = [item["ticker"] for item in body["items"]]
    assert "PYPL" in tickers
