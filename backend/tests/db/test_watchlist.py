"""Watchlist repository tests."""

from __future__ import annotations

import pytest

from app import db


def test_default_watchlist_seeded(temp_db: str) -> None:
    assert db.watchlist.list_tickers() == db.DEFAULT_WATCHLIST


def test_add_new_ticker(temp_db: str) -> None:
    assert db.watchlist.add_ticker("PYPL") is True
    assert "PYPL" in db.watchlist.list_tickers()


def test_add_duplicate_ticker_returns_false(temp_db: str) -> None:
    assert db.watchlist.add_ticker("AAPL") is False
    assert db.watchlist.list_tickers().count("AAPL") == 1


def test_add_normalizes_to_uppercase(temp_db: str) -> None:
    db.watchlist.add_ticker("  pypl  ")
    assert "PYPL" in db.watchlist.list_tickers()


def test_remove_existing_ticker(temp_db: str) -> None:
    assert db.watchlist.remove_ticker("AAPL") is True
    assert "AAPL" not in db.watchlist.list_tickers()


def test_remove_nonexistent_ticker_returns_false(temp_db: str) -> None:
    assert db.watchlist.remove_ticker("DOES_NOT_EXIST") is False


def test_add_empty_ticker_raises(temp_db: str) -> None:
    with pytest.raises(ValueError):
        db.watchlist.add_ticker("   ")
