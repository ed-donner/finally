"""Background snapshot task for periodic portfolio value recording."""

import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite

from app.market.cache import PriceCache

logger = logging.getLogger(__name__)

_snapshot_task: asyncio.Task | None = None


async def record_snapshot(db: aiosqlite.Connection, price_cache: PriceCache) -> None:
    """Record a portfolio value snapshot for the default user.

    Computes total_value = cash + sum(price * qty) for all positions.
    Positions without a current price in the cache are skipped.
    """
    row = await db.execute_fetchall(
        "SELECT cash_balance FROM users_profile WHERE id = ?", ("default",)
    )
    cash = row[0][0]

    positions = await db.execute_fetchall(
        "SELECT ticker, quantity FROM positions WHERE user_id = ?", ("default",)
    )

    market_value = 0.0
    for pos in positions:
        ticker, qty = pos[0], pos[1]
        price = price_cache.get_price(ticker)
        if price is not None:
            market_value += price * qty

    total_value = round(cash + market_value, 2)
    now = datetime.now(timezone.utc).isoformat()

    await db.execute(
        "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) VALUES (?, ?, ?, ?)",
        (str(uuid4()), "default", total_value, now),
    )


async def _snapshot_loop(
    db: aiosqlite.Connection, price_cache: PriceCache, interval: float
) -> None:
    """Internal loop that records snapshots at a fixed interval."""
    while True:
        try:
            await record_snapshot(db, price_cache)
        except Exception:
            logger.exception("Error recording portfolio snapshot")
        await asyncio.sleep(interval)


async def start_snapshot_task(
    db: aiosqlite.Connection, price_cache: PriceCache, interval: float = 30.0
) -> asyncio.Task:
    """Create a background task that records portfolio snapshots periodically."""
    global _snapshot_task
    _snapshot_task = asyncio.create_task(_snapshot_loop(db, price_cache, interval))
    return _snapshot_task


async def stop_snapshot_task() -> None:
    """Cancel the background snapshot task if running."""
    global _snapshot_task
    if _snapshot_task is None:
        return
    _snapshot_task.cancel()
    try:
        await _snapshot_task
    except asyncio.CancelledError:
        pass
    _snapshot_task = None
