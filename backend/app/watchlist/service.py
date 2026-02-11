"""Pure async DB functions for watchlist CRUD operations."""

import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite
from fastapi import HTTPException


async def get_watchlist(db: aiosqlite.Connection, user_id: str = "default") -> list[dict]:
    """Return all watchlist tickers for the user, ordered by added_at."""
    cursor = await db.execute(
        "SELECT ticker, added_at FROM watchlist WHERE user_id = ? ORDER BY added_at",
        (user_id,),
    )
    rows = await cursor.fetchall()
    return [{"ticker": row[0], "added_at": row[1]} for row in rows]


async def add_ticker(db: aiosqlite.Connection, ticker: str, user_id: str = "default") -> dict:
    """Add a ticker to the watchlist. Raises 409 if duplicate."""
    ticker = ticker.upper().strip()
    now = datetime.now(timezone.utc).isoformat()
    row_id = str(uuid4())

    try:
        await db.execute(
            "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
            (row_id, user_id, ticker, now),
        )
        await db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"{ticker} is already in the watchlist")

    return {"id": row_id, "ticker": ticker, "added_at": now}


async def remove_ticker(db: aiosqlite.Connection, ticker: str, user_id: str = "default") -> bool:
    """Remove a ticker from the watchlist. Raises 404 if not found."""
    ticker = ticker.upper().strip()
    cursor = await db.execute(
        "DELETE FROM watchlist WHERE user_id = ? AND ticker = ?",
        (user_id, ticker),
    )
    await db.commit()

    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"{ticker} is not in the watchlist")

    return True
