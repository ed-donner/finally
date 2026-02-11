"""Tests proving WAL mode, busy_timeout, concurrent access, and connection config."""

import asyncio
from uuid import uuid4

from app.db import init_db


async def test_wal_mode_enabled(db):
    cursor = await db.execute("PRAGMA journal_mode")
    row = await cursor.fetchone()
    assert row[0] == "wal"


async def test_busy_timeout_set(db):
    cursor = await db.execute("PRAGMA busy_timeout")
    row = await cursor.fetchone()
    assert row[0] == 5000


async def test_foreign_keys_enabled(db):
    cursor = await db.execute("PRAGMA foreign_keys")
    row = await cursor.fetchone()
    assert row[0] == 1


async def test_row_factory_returns_dict_like(db):
    cursor = await db.execute("SELECT cash_balance FROM users_profile WHERE id = ?", ("default",))
    row = await cursor.fetchone()
    assert row["cash_balance"] == 10000.0


async def test_concurrent_reads_and_writes(db):
    async def write_trade(i):
        await db.execute(
            "INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid4()), "default", "AAPL", "buy", 1.0, 150.0 + i, "2024-01-01T00:00:00"),
        )

    async def read_trades():
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM trades")
        await cursor.fetchone()

    tasks = [write_trade(i) for i in range(10)] + [read_trades() for _ in range(10)]
    await asyncio.gather(*tasks)

    # Verify all writes landed
    cursor = await db.execute("SELECT COUNT(*) as cnt FROM trades")
    row = await cursor.fetchone()
    assert row["cnt"] == 10


async def test_creates_parent_directory(tmp_path):
    nested_path = str(tmp_path / "sub" / "dir" / "test.db")
    conn = await init_db(nested_path)
    try:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = 'users_profile'"
        )
        row = await cursor.fetchone()
        assert row is not None
    finally:
        await conn.close()
