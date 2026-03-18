"""CRUD helper functions for all database tables."""

import json
import uuid
from datetime import datetime, timezone

from app.db.connection import get_connection

DEFAULT_USER = "default"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# users_profile
# ---------------------------------------------------------------------------

async def get_user_profile(user_id: str = DEFAULT_USER) -> dict | None:
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT id, cash_balance, created_at FROM users_profile WHERE id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def update_cash_balance(cash_balance: float, user_id: str = DEFAULT_USER) -> dict:
    db = await get_connection()
    try:
        await db.execute(
            "UPDATE users_profile SET cash_balance = ? WHERE id = ?",
            (cash_balance, user_id),
        )
        await db.commit()
        return await _get_user(db, user_id)
    finally:
        await db.close()


async def _get_user(db, user_id: str) -> dict:
    cursor = await db.execute(
        "SELECT id, cash_balance, created_at FROM users_profile WHERE id = ?",
        (user_id,),
    )
    row = await cursor.fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# watchlist
# ---------------------------------------------------------------------------

async def list_watchlist(user_id: str = DEFAULT_USER) -> list[dict]:
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT id, user_id, ticker, added_at FROM watchlist WHERE user_id = ? ORDER BY added_at",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def add_watchlist_ticker(ticker: str, user_id: str = DEFAULT_USER) -> dict:
    """Add a ticker to the watchlist. Raises ValueError on duplicate."""
    db = await get_connection()
    try:
        row_id = str(uuid.uuid4())
        now = _now()
        try:
            await db.execute(
                "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
                (row_id, user_id, ticker.upper(), now),
            )
            await db.commit()
        except Exception as e:
            if "UNIQUE constraint" in str(e):
                raise ValueError(f"Ticker {ticker.upper()} already in watchlist") from e
            raise
        return {"id": row_id, "user_id": user_id, "ticker": ticker.upper(), "added_at": now}
    finally:
        await db.close()


async def remove_watchlist_ticker(ticker: str, user_id: str = DEFAULT_USER) -> bool:
    """Remove a ticker from the watchlist. Returns True if removed, False if not found."""
    db = await get_connection()
    try:
        cursor = await db.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND ticker = ?",
            (user_id, ticker.upper()),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------

async def get_positions(user_id: str = DEFAULT_USER) -> list[dict]:
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT id, user_id, ticker, quantity, avg_cost, updated_at "
            "FROM positions WHERE user_id = ? ORDER BY ticker",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_position_by_ticker(ticker: str, user_id: str = DEFAULT_USER) -> dict | None:
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT id, user_id, ticker, quantity, avg_cost, updated_at "
            "FROM positions WHERE user_id = ? AND ticker = ?",
            (user_id, ticker.upper()),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def upsert_position(
    ticker: str, quantity: float, avg_cost: float, user_id: str = DEFAULT_USER
) -> dict:
    """Insert or update a position. If quantity is 0, delete it."""
    ticker = ticker.upper()
    now = _now()
    db = await get_connection()
    try:
        if quantity == 0:
            await db.execute(
                "DELETE FROM positions WHERE user_id = ? AND ticker = ?",
                (user_id, ticker),
            )
            await db.commit()
            return {"user_id": user_id, "ticker": ticker, "quantity": 0, "avg_cost": 0, "deleted": True}

        cursor = await db.execute(
            "SELECT id FROM positions WHERE user_id = ? AND ticker = ?",
            (user_id, ticker),
        )
        existing = await cursor.fetchone()

        if existing:
            await db.execute(
                "UPDATE positions SET quantity = ?, avg_cost = ?, updated_at = ? "
                "WHERE user_id = ? AND ticker = ?",
                (quantity, avg_cost, now, user_id, ticker),
            )
            row_id = existing["id"]
        else:
            row_id = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row_id, user_id, ticker, quantity, avg_cost, now),
            )

        await db.commit()
        return {
            "id": row_id,
            "user_id": user_id,
            "ticker": ticker,
            "quantity": quantity,
            "avg_cost": avg_cost,
            "updated_at": now,
        }
    finally:
        await db.close()


async def delete_position(ticker: str, user_id: str = DEFAULT_USER) -> bool:
    db = await get_connection()
    try:
        cursor = await db.execute(
            "DELETE FROM positions WHERE user_id = ? AND ticker = ?",
            (user_id, ticker.upper()),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# trades
# ---------------------------------------------------------------------------

async def insert_trade(
    ticker: str, side: str, quantity: float, price: float, user_id: str = DEFAULT_USER
) -> dict:
    """Record a trade. Side must be 'buy' or 'sell'."""
    if side not in ("buy", "sell"):
        raise ValueError(f"Invalid side: {side}")
    trade_id = str(uuid.uuid4())
    now = _now()
    db = await get_connection()
    try:
        await db.execute(
            "INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trade_id, user_id, ticker.upper(), side, quantity, price, now),
        )
        await db.commit()
        return {
            "id": trade_id,
            "user_id": user_id,
            "ticker": ticker.upper(),
            "side": side,
            "quantity": quantity,
            "price": price,
            "executed_at": now,
        }
    finally:
        await db.close()


async def list_trades(user_id: str = DEFAULT_USER, limit: int = 50) -> list[dict]:
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT id, user_id, ticker, side, quantity, price, executed_at "
            "FROM trades WHERE user_id = ? ORDER BY executed_at DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# portfolio_snapshots
# ---------------------------------------------------------------------------

async def insert_portfolio_snapshot(
    total_value: float, user_id: str = DEFAULT_USER
) -> dict:
    snap_id = str(uuid.uuid4())
    now = _now()
    db = await get_connection()
    try:
        await db.execute(
            "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            (snap_id, user_id, total_value, now),
        )
        await db.commit()
        return {
            "id": snap_id,
            "user_id": user_id,
            "total_value": total_value,
            "recorded_at": now,
        }
    finally:
        await db.close()


async def list_portfolio_snapshots(user_id: str = DEFAULT_USER, limit: int = 500) -> list[dict]:
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT id, user_id, total_value, recorded_at "
            "FROM portfolio_snapshots WHERE user_id = ? ORDER BY recorded_at DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# chat_messages
# ---------------------------------------------------------------------------

async def insert_chat_message(
    role: str, content: str, actions: dict | list | None = None, user_id: str = DEFAULT_USER
) -> dict:
    """Insert a chat message. actions is stored as JSON string."""
    if role not in ("user", "assistant"):
        raise ValueError(f"Invalid role: {role}")
    msg_id = str(uuid.uuid4())
    now = _now()
    actions_json = json.dumps(actions) if actions is not None else None
    db = await get_connection()
    try:
        await db.execute(
            "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, user_id, role, content, actions_json, now),
        )
        await db.commit()
        return {
            "id": msg_id,
            "user_id": user_id,
            "role": role,
            "content": content,
            "actions": actions,
            "created_at": now,
        }
    finally:
        await db.close()


async def get_recent_chat_messages(
    n: int = 20, user_id: str = DEFAULT_USER
) -> list[dict]:
    """Return the most recent N messages, ordered oldest-first."""
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT id, user_id, role, content, actions, created_at "
            "FROM chat_messages WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, n),
        )
        rows = await cursor.fetchall()
        result = []
        for r in reversed(rows):
            d = dict(r)
            if d["actions"] is not None:
                d["actions"] = json.loads(d["actions"])
            result.append(d)
        return result
    finally:
        await db.close()
