"""Tests for the watchlist service layer."""

import pytest
from fastapi import HTTPException

from app.watchlist.service import add_ticker, get_watchlist, remove_ticker


async def test_get_watchlist_returns_seed_data(db):
    """Seed data should contain 10 default tickers."""
    items = await get_watchlist(db)
    assert len(items) == 10
    for item in items:
        assert "ticker" in item
        assert "added_at" in item


async def test_add_ticker(db):
    """Adding a new ticker should persist it in the database."""
    result = await add_ticker(db, "PYPL")
    assert result["ticker"] == "PYPL"
    assert "added_at" in result

    # Verify it's in the database
    items = await get_watchlist(db)
    tickers = [item["ticker"] for item in items]
    assert "PYPL" in tickers


async def test_add_ticker_normalizes_case(db):
    """Ticker input should be uppercased and stripped."""
    result = await add_ticker(db, " pypl ")
    assert result["ticker"] == "PYPL"


async def test_add_duplicate_raises_409(db):
    """Adding a ticker that already exists should raise 409."""
    with pytest.raises(HTTPException) as exc_info:
        await add_ticker(db, "AAPL")
    assert exc_info.value.status_code == 409


async def test_remove_ticker(db):
    """Removing an existing ticker should delete it from the database."""
    result = await remove_ticker(db, "AAPL")
    assert result is True

    # Verify it's gone
    items = await get_watchlist(db)
    tickers = [item["ticker"] for item in items]
    assert "AAPL" not in tickers


async def test_remove_ticker_normalizes_case(db):
    """Removing with lowercase input should match the uppercase ticker."""
    result = await remove_ticker(db, "aapl")
    assert result is True

    items = await get_watchlist(db)
    tickers = [item["ticker"] for item in items]
    assert "AAPL" not in tickers


async def test_remove_nonexistent_raises_404(db):
    """Removing a ticker not in the watchlist should raise 404."""
    with pytest.raises(HTTPException) as exc_info:
        await remove_ticker(db, "FAKE")
    assert exc_info.value.status_code == 404
