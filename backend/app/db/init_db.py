"""Database initialization — create tables and seed default data."""

import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.db.connection import get_connection

SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]


async def init_db() -> None:
    """Create tables if missing and seed default data."""
    db = await get_connection()
    try:
        await db.executescript(SCHEMA_SQL)

        # Seed default user if not present
        row = await db.execute_fetchall(
            "SELECT id FROM users_profile WHERE id = ?", ("default",)
        )
        if not row:
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "INSERT INTO users_profile (id, cash_balance, created_at) VALUES (?, ?, ?)",
                ("default", 10000.0, now),
            )

        # Seed default watchlist entries if empty
        row = await db.execute_fetchall(
            "SELECT id FROM watchlist WHERE user_id = ?", ("default",)
        )
        if not row:
            now = datetime.now(timezone.utc).isoformat()
            for ticker in DEFAULT_TICKERS:
                await db.execute(
                    "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), "default", ticker, now),
                )

        await db.commit()
    finally:
        await db.close()
