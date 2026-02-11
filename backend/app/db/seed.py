"""Seed data for FinAlly database.

Inserts the default user profile and 10 watchlist tickers idempotently.
"""

from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]


async def seed_default_data(db: aiosqlite.Connection) -> None:
    """Seed default user and watchlist. Safe to call multiple times."""
    now = datetime.now(timezone.utc).isoformat()

    # Only create default user if they don't exist
    cursor = await db.execute("SELECT id FROM users_profile WHERE id = ?", ("default",))
    if await cursor.fetchone() is None:
        await db.execute(
            "INSERT INTO users_profile (id, cash_balance, created_at) VALUES (?, ?, ?)",
            ("default", 10000.0, now),
        )

    # Insert watchlist tickers, ignoring duplicates
    for ticker in DEFAULT_TICKERS:
        await db.execute(
            "INSERT OR IGNORE INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
            (str(uuid4()), "default", ticker, now),
        )

    await db.commit()
