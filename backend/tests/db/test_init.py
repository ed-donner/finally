"""Tests for schema init, seeding, idempotency, and path resolution."""

import sqlite3

import pytest

from app.db import get_conn, init_db
from app.db.connection import (
    _DEFAULT_TICKERS,
    _reset_for_tests,
    _resolve_db_path,
)


def test_creates_all_tables(db):
    conn = get_conn()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    tables = {row["name"] for row in rows}
    expected = {
        "users_profile",
        "watchlist",
        "positions",
        "trades",
        "portfolio_snapshots",
        "chat_messages",
    }
    assert expected <= tables


def test_seeds_default_profile(db):
    row = get_conn().execute(
        "SELECT id, cash_balance FROM users_profile WHERE id = 'default'"
    ).fetchone()
    assert row is not None
    assert row["id"] == "default"
    assert row["cash_balance"] == 10000.0


def test_seeds_ten_default_tickers_in_order(db):
    rows = get_conn().execute(
        "SELECT ticker FROM watchlist ORDER BY rowid"
    ).fetchall()
    tickers = [r["ticker"] for r in rows]
    assert tickers == _DEFAULT_TICKERS
    assert len(tickers) == 10


def test_row_factory_is_row(db):
    conn = get_conn()
    assert conn.row_factory is sqlite3.Row


def test_init_is_idempotent(db):
    # Re-running init must not duplicate seed data or error.
    init_db(str(db))
    init_db(str(db))
    profiles = get_conn().execute("SELECT COUNT(*) AS n FROM users_profile").fetchone()["n"]
    watch = get_conn().execute("SELECT COUNT(*) AS n FROM watchlist").fetchone()["n"]
    assert profiles == 1
    assert watch == 10


def test_seed_not_reapplied_after_user_edits(db, tmp_path):
    # Simulate a user emptying their watchlist, then re-init: seed must NOT return.
    conn = get_conn()
    conn.execute("DELETE FROM watchlist")
    conn.commit()
    conn.execute(
        "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES ('x', 'default', 'PYPL', 't')"
    )
    conn.commit()
    init_db(str(db))
    rows = get_conn().execute("SELECT ticker FROM watchlist").fetchall()
    tickers = [r["ticker"] for r in rows]
    assert tickers == ["PYPL"]


def test_resolve_db_path_arg_beats_env(monkeypatch):
    monkeypatch.setenv("FINALLY_DB_PATH", "env/path.db")
    resolved = _resolve_db_path("/tmp/explicit.db")
    assert resolved.replace("\\", "/").endswith("/tmp/explicit.db")


def test_resolve_db_path_env_used_when_no_arg(monkeypatch, tmp_path):
    target = tmp_path / "fromenv.db"
    monkeypatch.setenv("FINALLY_DB_PATH", str(target))
    assert _resolve_db_path() == str(target)


def test_resolve_db_path_default(monkeypatch):
    monkeypatch.delenv("FINALLY_DB_PATH", raising=False)
    resolved = _resolve_db_path().replace("\\", "/")
    assert resolved.endswith("db/finally.db")


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    _reset_for_tests()
