"""Watchlist repository."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime

from .connection import connect
from .init import DEFAULT_USER_ID


def _now() -> str:
    return datetime.now(UTC).isoformat()


def list_tickers(user_id: str = DEFAULT_USER_ID) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT ticker FROM watchlist WHERE user_id = ? ORDER BY added_at ASC",
            (user_id,),
        ).fetchall()
        return [r["ticker"] for r in rows]


def add_ticker(ticker: str, user_id: str = DEFAULT_USER_ID) -> bool:
    """Add a ticker. Returns True if newly added, False if it was already present."""
    ticker = ticker.upper().strip()
    if not ticker:
        raise ValueError("ticker must be non-empty")
    with connect() as conn:
        try:
            conn.execute(
                "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), user_id, ticker, _now()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def remove_ticker(ticker: str, user_id: str = DEFAULT_USER_ID) -> bool:
    """Remove a ticker. Returns True if a row was removed, False if it didn't exist."""
    ticker = ticker.upper().strip()
    with connect() as conn:
        cursor = conn.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND ticker = ?",
            (user_id, ticker),
        )
        return cursor.rowcount > 0
