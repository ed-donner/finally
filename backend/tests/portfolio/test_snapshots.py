"""Tests for portfolio snapshot recording and background task."""

import asyncio

from app.portfolio.service import execute_trade
from app.portfolio.snapshots import (
    record_snapshot,
    start_snapshot_task,
    stop_snapshot_task,
)


async def test_record_snapshot_cash_only(db, price_cache):
    """Snapshot with no positions should equal cash balance."""
    await record_snapshot(db, price_cache)

    rows = await db.execute_fetchall(
        "SELECT total_value FROM portfolio_snapshots WHERE user_id = 'default'"
    )
    assert len(rows) == 1
    assert rows[0][0] == 10000.0


async def test_record_snapshot_with_positions(db, price_cache):
    """Snapshot should include cash + market value of positions."""
    await execute_trade(db, price_cache, "AAPL", "buy", 10)
    # cash = 10000 - (150*10) = 8500, positions = 150*10 = 1500
    await record_snapshot(db, price_cache)

    rows = await db.execute_fetchall(
        "SELECT total_value FROM portfolio_snapshots WHERE user_id = 'default'"
    )
    assert len(rows) == 1
    assert rows[0][0] == 10000.0


async def test_record_snapshot_inserts_row(db, price_cache):
    """Each call to record_snapshot should insert one row."""
    await record_snapshot(db, price_cache)
    await record_snapshot(db, price_cache)

    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM portfolio_snapshots WHERE user_id = 'default'"
    )
    assert rows[0][0] == 2


async def test_start_stop_snapshot_task(db, price_cache):
    """Background task should record at least one snapshot then stop cleanly."""
    await start_snapshot_task(db, price_cache, interval=0.1)
    await asyncio.sleep(0.35)
    await stop_snapshot_task()

    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM portfolio_snapshots WHERE user_id = 'default'"
    )
    assert rows[0][0] >= 1


async def test_stop_snapshot_task_when_not_started():
    """Stopping when no task is running should not raise."""
    await stop_snapshot_task()
