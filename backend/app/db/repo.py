"""Repository functions — the data-access surface for API and LLM layers.

Every function accepts ``user_id`` (default ``"default"``) as a keyword and
operates on the thread-local connection returned by :func:`app.db.get_conn`.
Timestamps are ISO-8601 UTC strings; ids are ``uuid4().hex``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from .connection import get_conn

DEFAULT_USER_ID = "default"


def _utcnow() -> str:
    """Current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #
def get_profile(user_id: str = DEFAULT_USER_ID) -> dict:
    """Return the user profile: ``{id, cash_balance, created_at}``."""
    row = get_conn().execute(
        "SELECT id, cash_balance, created_at FROM users_profile WHERE id = ?",
        (user_id,),
    ).fetchone()
    return dict(row) if row else {}


def set_cash_balance(cash: float, user_id: str = DEFAULT_USER_ID) -> None:
    """Set the user's cash balance."""
    conn = get_conn()
    conn.execute(
        "UPDATE users_profile SET cash_balance = ? WHERE id = ?",
        (cash, user_id),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Watchlist
# --------------------------------------------------------------------------- #
def get_watchlist(user_id: str = DEFAULT_USER_ID) -> list[str]:
    """Return watched tickers in insertion order."""
    rows = get_conn().execute(
        "SELECT ticker FROM watchlist WHERE user_id = ? ORDER BY rowid",
        (user_id,),
    ).fetchall()
    return [row["ticker"] for row in rows]


def add_watchlist(ticker: str, user_id: str = DEFAULT_USER_ID) -> bool:
    """Add a ticker. Returns False if it was already present."""
    conn = get_conn()
    exists = conn.execute(
        "SELECT 1 FROM watchlist WHERE user_id = ? AND ticker = ?",
        (user_id, ticker),
    ).fetchone()
    if exists is not None:
        return False
    conn.execute(
        "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
        (uuid4().hex, user_id, ticker, _utcnow()),
    )
    conn.commit()
    return True


def remove_watchlist(ticker: str, user_id: str = DEFAULT_USER_ID) -> bool:
    """Remove a ticker. Returns False if it was absent."""
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM watchlist WHERE user_id = ? AND ticker = ?",
        (user_id, ticker),
    )
    conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Positions
# --------------------------------------------------------------------------- #
def get_positions(user_id: str = DEFAULT_USER_ID) -> list[dict]:
    """Return all positions: ``[{ticker, quantity, avg_cost, updated_at}]``."""
    rows = get_conn().execute(
        "SELECT ticker, quantity, avg_cost, updated_at FROM positions "
        "WHERE user_id = ? ORDER BY rowid",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_position(ticker: str, user_id: str = DEFAULT_USER_ID) -> dict | None:
    """Return a single position, or None if not held."""
    row = get_conn().execute(
        "SELECT ticker, quantity, avg_cost, updated_at FROM positions "
        "WHERE user_id = ? AND ticker = ?",
        (user_id, ticker),
    ).fetchone()
    return dict(row) if row else None


def upsert_position(
    ticker: str,
    quantity: float,
    avg_cost: float,
    user_id: str = DEFAULT_USER_ID,
) -> None:
    """Insert or update a position (keyed on user_id + ticker)."""
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (user_id, ticker) DO UPDATE SET
            quantity = excluded.quantity,
            avg_cost = excluded.avg_cost,
            updated_at = excluded.updated_at
        """,
        (uuid4().hex, user_id, ticker, quantity, avg_cost, _utcnow()),
    )
    conn.commit()


def delete_position(ticker: str, user_id: str = DEFAULT_USER_ID) -> None:
    """Remove a position (e.g. after selling the full holding)."""
    conn = get_conn()
    conn.execute(
        "DELETE FROM positions WHERE user_id = ? AND ticker = ?",
        (user_id, ticker),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Trades
# --------------------------------------------------------------------------- #
def insert_trade(
    ticker: str,
    side: str,
    quantity: float,
    price: float,
    user_id: str = DEFAULT_USER_ID,
) -> dict:
    """Append a trade to the log and return the full inserted row."""
    conn = get_conn()
    trade_id = uuid4().hex
    executed_at = _utcnow()
    conn.execute(
        """
        INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (trade_id, user_id, ticker, side, quantity, price, executed_at),
    )
    conn.commit()
    return {
        "id": trade_id,
        "user_id": user_id,
        "ticker": ticker,
        "side": side,
        "quantity": quantity,
        "price": price,
        "executed_at": executed_at,
    }


def get_trades(limit: int = 100, user_id: str = DEFAULT_USER_ID) -> list[dict]:
    """Return the most recent trades (newest first), up to ``limit``."""
    rows = get_conn().execute(
        "SELECT id, user_id, ticker, side, quantity, price, executed_at FROM trades "
        "WHERE user_id = ? ORDER BY executed_at DESC, rowid DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Portfolio snapshots
# --------------------------------------------------------------------------- #
def insert_snapshot(total_value: float, user_id: str = DEFAULT_USER_ID) -> None:
    """Record a portfolio total-value snapshot."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) "
        "VALUES (?, ?, ?, ?)",
        (uuid4().hex, user_id, total_value, _utcnow()),
    )
    conn.commit()


def get_snapshots(limit: int = 500, user_id: str = DEFAULT_USER_ID) -> list[dict]:
    """Return up to ``limit`` most recent snapshots, ascending by time.

    ``[{total_value, recorded_at}]``.
    """
    rows = get_conn().execute(
        """
        SELECT total_value, recorded_at FROM (
            SELECT total_value, recorded_at, rowid FROM portfolio_snapshots
            WHERE user_id = ?
            ORDER BY recorded_at DESC, rowid DESC
            LIMIT ?
        ) ORDER BY recorded_at ASC, rowid ASC
        """,
        (user_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Chat messages
# --------------------------------------------------------------------------- #
def get_chat_messages(limit: int = 50, user_id: str = DEFAULT_USER_ID) -> list[dict]:
    """Return up to ``limit`` most recent chat messages, ascending by time.

    ``[{role, content, actions, created_at}]`` where ``actions`` is a decoded
    dict (or None).
    """
    rows = get_conn().execute(
        """
        SELECT role, content, actions, created_at FROM (
            SELECT role, content, actions, created_at, rowid FROM chat_messages
            WHERE user_id = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
        ) ORDER BY created_at ASC, rowid ASC
        """,
        (user_id, limit),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["actions"] = json.loads(item["actions"]) if item["actions"] else None
        result.append(item)
    return result


def insert_chat_message(
    role: str,
    content: str,
    actions: dict | None,
    user_id: str = DEFAULT_USER_ID,
) -> None:
    """Append a chat message; ``actions`` is JSON-encoded (or NULL)."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            uuid4().hex,
            user_id,
            role,
            content,
            json.dumps(actions) if actions is not None else None,
            _utcnow(),
        ),
    )
    conn.commit()
