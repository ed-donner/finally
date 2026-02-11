"""CRUD operations for all database tables."""

import uuid
from datetime import datetime, timezone

from .connection import get_connection
from .schema import DEFAULT_USER_ID


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


# --- Users Profile ---


async def get_cash_balance(user_id: str = DEFAULT_USER_ID) -> float:
    """Get the user's cash balance."""
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT cash_balance FROM users_profile WHERE id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row["cash_balance"] if row else 0.0
    finally:
        await db.close()


async def update_cash_balance(amount: float, user_id: str = DEFAULT_USER_ID) -> float:
    """Set the user's cash balance to the given amount. Returns the new balance."""
    db = await get_connection()
    try:
        await db.execute(
            "UPDATE users_profile SET cash_balance = ? WHERE id = ?", (amount, user_id)
        )
        await db.commit()
        return amount
    finally:
        await db.close()


# --- Watchlist ---


async def get_watchlist(user_id: str = DEFAULT_USER_ID) -> list[dict]:
    """Get all watchlist tickers for a user."""
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT id, ticker, added_at FROM watchlist WHERE user_id = ? ORDER BY added_at",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def add_to_watchlist(ticker: str, user_id: str = DEFAULT_USER_ID) -> dict:
    """Add a ticker to the watchlist. Returns the new entry."""
    ticker = ticker.upper()
    entry = {"id": _uuid(), "user_id": user_id, "ticker": ticker, "added_at": _now()}
    db = await get_connection()
    try:
        await db.execute(
            "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
            (entry["id"], user_id, ticker, entry["added_at"]),
        )
        await db.commit()
        return entry
    finally:
        await db.close()


async def remove_from_watchlist(ticker: str, user_id: str = DEFAULT_USER_ID) -> bool:
    """Remove a ticker from the watchlist. Returns True if a row was deleted."""
    ticker = ticker.upper()
    db = await get_connection()
    try:
        cursor = await db.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND ticker = ?", (user_id, ticker)
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


# --- Positions ---


async def get_positions(user_id: str = DEFAULT_USER_ID) -> list[dict]:
    """Get all positions for a user."""
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT id, ticker, quantity, avg_cost, updated_at FROM positions "
            "WHERE user_id = ? ORDER BY ticker",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def get_position(ticker: str, user_id: str = DEFAULT_USER_ID) -> dict | None:
    """Get a single position by ticker."""
    ticker = ticker.upper()
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT id, ticker, quantity, avg_cost, updated_at FROM positions "
            "WHERE user_id = ? AND ticker = ?",
            (user_id, ticker),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def upsert_position(
    ticker: str, quantity: float, avg_cost: float, user_id: str = DEFAULT_USER_ID
) -> dict:
    """Create or update a position. Returns the position dict."""
    ticker = ticker.upper()
    now = _now()
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT id FROM positions WHERE user_id = ? AND ticker = ?", (user_id, ticker)
        )
        existing = await cursor.fetchone()

        if existing:
            await db.execute(
                "UPDATE positions SET quantity = ?, avg_cost = ?, updated_at = ? WHERE id = ?",
                (quantity, avg_cost, now, existing["id"]),
            )
            pos_id = existing["id"]
        else:
            pos_id = _uuid()
            await db.execute(
                "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pos_id, user_id, ticker, quantity, avg_cost, now),
            )

        await db.commit()
        return {
            "id": pos_id,
            "ticker": ticker,
            "quantity": quantity,
            "avg_cost": avg_cost,
            "updated_at": now,
        }
    finally:
        await db.close()


async def delete_position(ticker: str, user_id: str = DEFAULT_USER_ID) -> bool:
    """Delete a position. Returns True if a row was deleted."""
    ticker = ticker.upper()
    db = await get_connection()
    try:
        cursor = await db.execute(
            "DELETE FROM positions WHERE user_id = ? AND ticker = ?", (user_id, ticker)
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


# --- Trades ---


async def insert_trade(
    ticker: str, side: str, quantity: float, price: float, user_id: str = DEFAULT_USER_ID
) -> dict:
    """Record a trade. Returns the trade dict."""
    ticker = ticker.upper()
    trade = {
        "id": _uuid(),
        "user_id": user_id,
        "ticker": ticker,
        "side": side,
        "quantity": quantity,
        "price": price,
        "executed_at": _now(),
    }
    db = await get_connection()
    try:
        await db.execute(
            "INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trade["id"], user_id, ticker, side, quantity, price, trade["executed_at"]),
        )
        await db.commit()
        return trade
    finally:
        await db.close()


# --- Portfolio Snapshots ---


async def insert_snapshot(total_value: float, user_id: str = DEFAULT_USER_ID) -> dict:
    """Record a portfolio value snapshot."""
    snapshot = {
        "id": _uuid(),
        "user_id": user_id,
        "total_value": total_value,
        "recorded_at": _now(),
    }
    db = await get_connection()
    try:
        await db.execute(
            "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            (snapshot["id"], user_id, total_value, snapshot["recorded_at"]),
        )
        await db.commit()
        return snapshot
    finally:
        await db.close()


async def get_portfolio_history(user_id: str = DEFAULT_USER_ID) -> list[dict]:
    """Get portfolio value snapshots ordered by time."""
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT id, total_value, recorded_at FROM portfolio_snapshots "
            "WHERE user_id = ? ORDER BY recorded_at",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


# --- Chat Messages ---


async def insert_chat_message(
    role: str, content: str, actions: str | None = None, user_id: str = DEFAULT_USER_ID
) -> dict:
    """Store a chat message. actions is a JSON string or None."""
    msg = {
        "id": _uuid(),
        "user_id": user_id,
        "role": role,
        "content": content,
        "actions": actions,
        "created_at": _now(),
    }
    db = await get_connection()
    try:
        await db.execute(
            "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (msg["id"], user_id, role, content, actions, msg["created_at"]),
        )
        await db.commit()
        return msg
    finally:
        await db.close()


async def get_chat_history(limit: int = 50, user_id: str = DEFAULT_USER_ID) -> list[dict]:
    """Get recent chat messages ordered oldest-first."""
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT id, role, content, actions, created_at FROM chat_messages "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        # Reverse so oldest is first
        return [dict(row) for row in reversed(rows)]
    finally:
        await db.close()
