"""Tests for watchlist service."""

import tempfile
from pathlib import Path

import pytest

from app.db.database import init_db
from app.market import PriceCache
from app.watchlist.service import WatchlistService


@pytest.fixture
def setup():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(tmp.name)
    tmp.close()
    db_path.unlink()
    conn = init_db(db_path)
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    cache.update("GOOGL", 175.00)
    service = WatchlistService(conn, cache)
    return service


class TestWatchlist:
    def test_default_tickers(self, setup):
        tickers = setup.get_tickers()
        assert len(tickers) == 10
        assert "AAPL" in tickers

    def test_get_watchlist_with_prices(self, setup):
        wl = setup.get_watchlist()
        aapl = next(w for w in wl if w["ticker"] == "AAPL")
        assert aapl["price"] == 190.00

    def test_get_watchlist_no_price(self, setup):
        wl = setup.get_watchlist()
        # Tickers without cached prices
        jpm = next(w for w in wl if w["ticker"] == "JPM")
        assert jpm["price"] is None

    def test_add_ticker(self, setup):
        result = setup.add_ticker("PYPL")
        assert result["ticker"] == "PYPL"
        assert "PYPL" in setup.get_tickers()

    def test_add_duplicate(self, setup):
        with pytest.raises(ValueError, match="already in watchlist"):
            setup.add_ticker("AAPL")

    def test_remove_ticker(self, setup):
        setup.remove_ticker("AAPL")
        assert "AAPL" not in setup.get_tickers()

    def test_remove_nonexistent(self, setup):
        with pytest.raises(ValueError, match="not in watchlist"):
            setup.remove_ticker("PYPL")

    def test_add_normalizes_case(self, setup):
        setup.add_ticker("pypl")
        assert "PYPL" in setup.get_tickers()
