"""Watchlist service: CRUD operations with live prices."""

import uuid
from datetime import datetime, timezone
from sqlite3 import Connection

from app.market import PriceCache


class WatchlistService:
    """Manages watchlist operations."""

    def __init__(self, db: Connection, price_cache: PriceCache, user_id: str = "default") -> None:
        self.db = db
        self.price_cache = price_cache
        self.user_id = user_id

    def get_watchlist(self) -> list[dict]:
        """Get all watched tickers with latest prices."""
        rows = self.db.execute(
            "SELECT ticker, added_at FROM watchlist WHERE user_id = ? ORDER BY added_at",
            (self.user_id,),
        ).fetchall()
        result = []
        for r in rows:
            update = self.price_cache.get(r["ticker"])
            entry = {
                "ticker": r["ticker"],
                "added_at": r["added_at"],
                "price": update.price if update else None,
                "previous_price": update.previous_price if update else None,
                "change": update.change if update else None,
                "change_percent": update.change_percent if update else None,
                "direction": update.direction if update else None,
            }
            result.append(entry)
        return result

    def add_ticker(self, ticker: str) -> dict:
        """Add a ticker to the watchlist. Raises ValueError if already exists."""
        ticker = ticker.upper()
        existing = self.db.execute(
            "SELECT id FROM watchlist WHERE user_id = ? AND ticker = ?",
            (self.user_id, ticker),
        ).fetchone()
        if existing:
            raise ValueError(f"{ticker} is already in watchlist")

        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), self.user_id, ticker, now),
        )
        self.db.commit()
        return {"ticker": ticker, "added_at": now}

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the watchlist. Raises ValueError if not found."""
        ticker = ticker.upper()
        cursor = self.db.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND ticker = ?",
            (self.user_id, ticker),
        )
        self.db.commit()
        if cursor.rowcount == 0:
            raise ValueError(f"{ticker} not in watchlist")

    def get_tickers(self) -> list[str]:
        """Get just the ticker symbols."""
        rows = self.db.execute(
            "SELECT ticker FROM watchlist WHERE user_id = ? ORDER BY added_at",
            (self.user_id,),
        ).fetchall()
        return [r["ticker"] for r in rows]
