"""Tests proving seed data is correct and idempotent."""

from app.db import init_db
from app.db.seed import DEFAULT_TICKERS


async def test_default_user_created(db):
    cursor = await db.execute("SELECT cash_balance FROM users_profile WHERE id = ?", ("default",))
    row = await cursor.fetchone()
    assert row is not None
    assert row["cash_balance"] == 10000.0


async def test_ten_watchlist_tickers(db):
    cursor = await db.execute(
        "SELECT ticker FROM watchlist WHERE user_id = ? ORDER BY ticker", ("default",)
    )
    rows = await cursor.fetchall()
    tickers = [row["ticker"] for row in rows]
    assert len(tickers) == 10
    assert set(tickers) == set(DEFAULT_TICKERS)


async def test_seed_is_idempotent(db, db_path):
    # db fixture already called init_db once; call it again on same path
    conn2 = await init_db(db_path)
    try:
        cursor = await conn2.execute(
            "SELECT cash_balance FROM users_profile WHERE id = ?", ("default",)
        )
        row = await cursor.fetchone()
        assert row["cash_balance"] == 10000.0

        cursor = await conn2.execute(
            "SELECT COUNT(*) as cnt FROM watchlist WHERE user_id = ?", ("default",)
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 10
    finally:
        await conn2.close()


async def test_existing_data_preserved(db, db_path):
    # Modify cash balance
    await db.execute(
        "UPDATE users_profile SET cash_balance = ? WHERE id = ?", (5000.0, "default")
    )
    await db.commit()

    # Re-init on same path
    conn2 = await init_db(db_path)
    try:
        cursor = await conn2.execute(
            "SELECT cash_balance FROM users_profile WHERE id = ?", ("default",)
        )
        row = await cursor.fetchone()
        assert row["cash_balance"] == 5000.0  # Not reset to 10000
    finally:
        await conn2.close()
