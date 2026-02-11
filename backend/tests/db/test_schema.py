"""Tests proving all 6 tables are created with correct schema."""

import sqlite3

import pytest


EXPECTED_TABLES = {
    "users_profile",
    "watchlist",
    "positions",
    "trades",
    "portfolio_snapshots",
    "chat_messages",
}


async def test_all_six_tables_created(db):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    rows = await cursor.fetchall()
    table_names = {row["name"] for row in rows}
    assert table_names == EXPECTED_TABLES


async def test_users_profile_columns(db):
    cursor = await db.execute("PRAGMA table_info(users_profile)")
    rows = await cursor.fetchall()
    column_names = {row["name"] for row in rows}
    assert column_names == {"id", "cash_balance", "created_at"}


async def test_watchlist_unique_constraint(db):
    await db.execute(
        "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
        ("id1", "test", "AAPL", "2024-01-01T00:00:00"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
            ("id2", "test", "AAPL", "2024-01-01T00:00:00"),
        )


async def test_positions_unique_constraint(db):
    await db.execute(
        "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("id1", "test", "AAPL", 10.0, 150.0, "2024-01-01T00:00:00"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("id2", "test", "AAPL", 5.0, 160.0, "2024-01-01T00:00:00"),
        )
