"""Lazy initialization, idempotency, and seed-data tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.db.connection import connect


def test_first_init_creates_schema_and_seeds(temp_db: str) -> None:
    assert db.profile.get_cash_balance() == db.DEFAULT_CASH_BALANCE
    tickers = db.watchlist.list_tickers()
    assert tickers == db.DEFAULT_WATCHLIST


def test_init_is_idempotent(temp_db: str) -> None:
    db.ensure_initialized()
    db.ensure_initialized()
    db.reset_initialization_state()
    db.ensure_initialized()
    # Cash + watchlist remain pristine after repeated initialization
    assert db.profile.get_cash_balance() == db.DEFAULT_CASH_BALANCE
    assert db.watchlist.list_tickers() == db.DEFAULT_WATCHLIST


def test_init_does_not_overwrite_existing_data(temp_db: str) -> None:
    db.profile.set_cash_balance(7777.0)
    db.watchlist.remove_ticker("AAPL")
    db.reset_initialization_state()
    db.ensure_initialized()
    assert db.profile.get_cash_balance() == 7777.0
    assert "AAPL" not in db.watchlist.list_tickers()


def test_creates_parent_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nested = tmp_path / "deep" / "nested" / "dir" / "x.db"
    monkeypatch.setenv("FINALLY_DB_PATH", str(nested))
    db.reset_initialization_state()
    db.ensure_initialized()
    assert nested.exists()


def test_all_tables_created(temp_db: str) -> None:
    expected = {
        "user_profile", "watchlist", "positions",
        "trades", "portfolio_snapshots", "chat_messages",
    }
    with connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert expected.issubset(names)
