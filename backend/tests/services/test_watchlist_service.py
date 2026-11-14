"""Tests for app.services.watchlist."""

from __future__ import annotations

import pytest

from app import db
from app.services.portfolio import execute_trade
from app.services.watchlist import (
    DuplicateTickerError,
    InvalidTickerError,
    add_ticker,
    get_watchlist_with_prices,
    remove_ticker,
    sync_tracked_tickers,
)

pytestmark = pytest.mark.usefixtures("temp_db")

DEFAULTS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]


def test_watchlist_with_prices(cache):
    items = get_watchlist_with_prices(cache)
    assert [i["ticker"] for i in items] == DEFAULTS
    aapl = items[0]
    assert aapl["price"] == 100.0
    assert aapl["previous_price"] == 100.0
    assert aapl["direction"] == "flat"
    assert set(aapl) == {
        "ticker", "price", "previous_price", "change_percent",
        "day_change_percent", "direction", "timestamp",
    }
    googl = items[1]
    assert googl["price"] is None and googl["direction"] is None


async def test_add_ticker(cache, source):
    result = await add_ticker(" pypl ", source)
    assert result == {"ticker": "PYPL"}
    assert "PYPL" in db.get_watchlist()
    assert "PYPL" in source.get_tickers()
    assert cache.get_price("PYPL") is not None


async def test_add_duplicate(source):
    with pytest.raises(DuplicateTickerError):
        await add_ticker("AAPL", source)


@pytest.mark.parametrize("bad", ["", "  ", "123", "WAYTOOLONGTICKER", "AB CD"])
async def test_add_invalid(source, bad):
    with pytest.raises(InvalidTickerError):
        await add_ticker(bad, source)
    with pytest.raises(ValueError):  # contract: ValueError subclass
        await add_ticker(bad, source)


async def test_remove_ticker(source):
    await sync_tracked_tickers(source)
    assert await remove_ticker("msft", source) is True
    assert "MSFT" not in db.get_watchlist()
    assert "MSFT" not in source.get_tickers()
    assert await remove_ticker("MSFT", source) is False


async def test_remove_keeps_tracking_held_position(cache, source):
    execute_trade("AAPL", "buy", 1, cache)
    await sync_tracked_tickers(source)
    assert await remove_ticker("AAPL", source) is True
    assert "AAPL" in source.get_tickers()


async def test_sync_tracks_watchlist_union_positions(cache, source):
    db.remove_watchlist_ticker("NVDA")
    execute_trade("NVDA", "buy", 1, cache)
    await sync_tracked_tickers(source)
    assert set(source.get_tickers()) == set(DEFAULTS)
    execute_trade("NVDA", "sell", 1, cache)
    await sync_tracked_tickers(source)
    assert "NVDA" not in source.get_tickers()
