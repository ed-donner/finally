"""Tests for watchlist endpoints."""


async def test_get_watchlist(app_client):
    """Default watchlist has 10 seeded tickers."""
    resp = await app_client.get("/api/watchlist")
    assert resp.status_code == 200
    data = resp.json()
    tickers = [w["ticker"] for w in data]
    assert "AAPL" in tickers
    assert len(tickers) == 10


async def test_watchlist_enriched_with_prices(app_client):
    """Watchlist entries include price data when available."""
    resp = await app_client.get("/api/watchlist")
    data = resp.json()
    aapl = next(w for w in data if w["ticker"] == "AAPL")
    assert "price" in aapl
    assert aapl["price"] == 190.0


async def test_add_ticker(app_client):
    """Add a new ticker to the watchlist."""
    resp = await app_client.post("/api/watchlist", json={"ticker": "PYPL"})
    assert resp.status_code == 200
    assert resp.json()["ticker"] == "PYPL"

    # Verify it appears in the list
    resp = await app_client.get("/api/watchlist")
    tickers = [w["ticker"] for w in resp.json()]
    assert "PYPL" in tickers


async def test_add_duplicate_ticker(app_client):
    """Adding an existing ticker returns 409."""
    resp = await app_client.post("/api/watchlist", json={"ticker": "AAPL"})
    assert resp.status_code == 409


async def test_add_invalid_ticker(app_client):
    """Invalid ticker format is rejected."""
    resp = await app_client.post("/api/watchlist", json={"ticker": "toolong"})
    assert resp.status_code == 400 or resp.status_code == 422


async def test_delete_ticker(app_client):
    """Remove a ticker from the watchlist."""
    resp = await app_client.delete("/api/watchlist/AAPL")
    assert resp.status_code == 200
    assert resp.json()["removed"] == "AAPL"

    # Verify it's gone
    resp = await app_client.get("/api/watchlist")
    tickers = [w["ticker"] for w in resp.json()]
    assert "AAPL" not in tickers


async def test_delete_nonexistent_ticker(app_client):
    """Deleting a ticker not in watchlist returns 404."""
    resp = await app_client.delete("/api/watchlist/ZZZZ")
    assert resp.status_code == 404


async def test_add_ticker_lowercase_normalized(app_client):
    """Lowercase input is normalized to uppercase."""
    resp = await app_client.post("/api/watchlist", json={"ticker": "pypl"})
    assert resp.status_code == 200
    assert resp.json()["ticker"] == "PYPL"
