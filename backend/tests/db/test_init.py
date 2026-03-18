"""Tests for database initialization and seeding."""

from app.db.connection import get_connection
from app.db.init_db import DEFAULT_TICKERS


async def test_tables_created():
    """All 6 tables should exist after init."""
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        rows = await cursor.fetchall()
        tables = {r["name"] for r in rows}
        expected = {
            "users_profile", "watchlist", "positions",
            "trades", "portfolio_snapshots", "chat_messages",
        }
        assert expected.issubset(tables)
    finally:
        await db.close()


async def test_default_user_seeded():
    """Default user should have $10k cash."""
    db = await get_connection()
    try:
        cursor = await db.execute("SELECT * FROM users_profile WHERE id = 'default'")
        row = await cursor.fetchone()
        assert row is not None
        assert row["cash_balance"] == 10000.0
    finally:
        await db.close()


async def test_default_watchlist_seeded():
    """Default watchlist should contain 10 tickers."""
    db = await get_connection()
    try:
        cursor = await db.execute("SELECT ticker FROM watchlist WHERE user_id = 'default'")
        rows = await cursor.fetchall()
        tickers = {r["ticker"] for r in rows}
        assert tickers == set(DEFAULT_TICKERS)
    finally:
        await db.close()


async def test_init_idempotent():
    """Running init_db a second time should not duplicate data."""
    from app.db.init_db import init_db
    await init_db()
    db = await get_connection()
    try:
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM users_profile")
        row = await cursor.fetchone()
        assert row["cnt"] == 1

        cursor = await db.execute("SELECT COUNT(*) as cnt FROM watchlist")
        row = await cursor.fetchone()
        assert row["cnt"] == 10
    finally:
        await db.close()
