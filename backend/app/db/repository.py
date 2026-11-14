"""Repository functions for all FinAlly tables.

Every function accepts an optional ``conn``; pass the connection from ``transaction()``
to compose several writes atomically. Without it, each call uses its own connection.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .connection import DEFAULT_USER, new_id, now_iso, use_connection

Conn = sqlite3.Connection | None


def _norm(ticker: str) -> str:
    return ticker.strip().upper()


# --- users_profile ---------------------------------------------------------------------------


def get_cash_balance(*, user_id: str = DEFAULT_USER, conn: Conn = None) -> float:
    """Cash balance for the user (0.0 if the user has no profile)."""
    with use_connection(conn) as c:
        row = c.execute(
            "SELECT cash_balance FROM users_profile WHERE id = ?", (user_id,)
        ).fetchone()
    return float(row["cash_balance"]) if row else 0.0


def set_cash_balance(amount: float, *, user_id: str = DEFAULT_USER, conn: Conn = None) -> None:
    """Set the cash balance, creating the profile row if missing."""
    with use_connection(conn) as c:
        c.execute(
            "INSERT INTO users_profile (id, cash_balance, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET cash_balance = excluded.cash_balance",
            (user_id, float(amount), now_iso()),
        )


# --- watchlist -------------------------------------------------------------------------------


def get_watchlist(*, user_id: str = DEFAULT_USER, conn: Conn = None) -> list[str]:
    """Watched tickers, ordered by when they were added."""
    with use_connection(conn) as c:
        rows = c.execute(
            "SELECT ticker FROM watchlist WHERE user_id = ? ORDER BY added_at, rowid",
            (user_id,),
        ).fetchall()
    return [r["ticker"] for r in rows]


def add_watchlist_ticker(ticker: str, *, user_id: str = DEFAULT_USER, conn: Conn = None) -> bool:
    """Add a ticker. Returns False if it was already present."""
    with use_connection(conn) as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
            (new_id(), user_id, _norm(ticker), now_iso()),
        )
    return cur.rowcount == 1


def remove_watchlist_ticker(
    ticker: str, *, user_id: str = DEFAULT_USER, conn: Conn = None
) -> bool:
    """Remove a ticker. Returns False if it was not present."""
    with use_connection(conn) as c:
        cur = c.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND ticker = ?", (user_id, _norm(ticker))
        )
    return cur.rowcount > 0


# --- positions -------------------------------------------------------------------------------

_POSITION_COLS = "ticker, quantity, avg_cost, updated_at"


def get_positions(*, user_id: str = DEFAULT_USER, conn: Conn = None) -> list[dict[str, Any]]:
    """All positions as ``{ticker, quantity, avg_cost, updated_at}``, ordered by ticker."""
    with use_connection(conn) as c:
        rows = c.execute(
            f"SELECT {_POSITION_COLS} FROM positions WHERE user_id = ? ORDER BY ticker",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_position(
    ticker: str, *, user_id: str = DEFAULT_USER, conn: Conn = None
) -> dict[str, Any] | None:
    with use_connection(conn) as c:
        row = c.execute(
            f"SELECT {_POSITION_COLS} FROM positions WHERE user_id = ? AND ticker = ?",
            (user_id, _norm(ticker)),
        ).fetchone()
    return dict(row) if row else None


def upsert_position(
    ticker: str,
    quantity: float,
    avg_cost: float,
    *,
    user_id: str = DEFAULT_USER,
    conn: Conn = None,
) -> None:
    """Insert or replace the quantity/avg_cost for a ticker."""
    with use_connection(conn) as c:
        c.execute(
            "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, ticker) DO UPDATE SET quantity = excluded.quantity, "
            "avg_cost = excluded.avg_cost, updated_at = excluded.updated_at",
            (new_id(), user_id, _norm(ticker), float(quantity), float(avg_cost), now_iso()),
        )


def delete_position(ticker: str, *, user_id: str = DEFAULT_USER, conn: Conn = None) -> None:
    with use_connection(conn) as c:
        c.execute(
            "DELETE FROM positions WHERE user_id = ? AND ticker = ?", (user_id, _norm(ticker))
        )


# --- trades ----------------------------------------------------------------------------------


def record_trade(
    ticker: str,
    side: str,
    quantity: float,
    price: float,
    *,
    user_id: str = DEFAULT_USER,
    conn: Conn = None,
) -> dict[str, Any]:
    """Append a trade. Returns ``{id, ticker, side, quantity, price, executed_at}``."""
    side = side.strip().lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"Invalid side: {side!r}")
    trade = {
        "id": new_id(),
        "ticker": _norm(ticker),
        "side": side,
        "quantity": float(quantity),
        "price": float(price),
        "executed_at": now_iso(),
    }
    with use_connection(conn) as c:
        c.execute(
            "INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                trade["id"],
                user_id,
                trade["ticker"],
                trade["side"],
                trade["quantity"],
                trade["price"],
                trade["executed_at"],
            ),
        )
    return trade


def get_trades(
    *, limit: int = 100, user_id: str = DEFAULT_USER, conn: Conn = None
) -> list[dict[str, Any]]:
    """Most recent trades, newest first."""
    with use_connection(conn) as c:
        rows = c.execute(
            "SELECT id, ticker, side, quantity, price, executed_at FROM trades "
            "WHERE user_id = ? ORDER BY executed_at DESC, rowid DESC LIMIT ?",
            (user_id, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


# --- portfolio_snapshots ---------------------------------------------------------------------


def record_snapshot(
    total_value: float, *, user_id: str = DEFAULT_USER, conn: Conn = None
) -> dict[str, Any]:
    """Record a portfolio value snapshot. Returns ``{id, total_value, recorded_at}``."""
    snap = {"id": new_id(), "total_value": float(total_value), "recorded_at": now_iso()}
    with use_connection(conn) as c:
        c.execute(
            "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            (snap["id"], user_id, snap["total_value"], snap["recorded_at"]),
        )
    return snap


def get_snapshots(
    *, limit: int = 1000, user_id: str = DEFAULT_USER, conn: Conn = None
) -> list[dict[str, Any]]:
    """The most recent ``limit`` snapshots, oldest first: ``{total_value, recorded_at}``."""
    with use_connection(conn) as c:
        rows = c.execute(
            "SELECT total_value, recorded_at FROM ("
            "  SELECT total_value, recorded_at, rowid AS rid FROM portfolio_snapshots"
            "  WHERE user_id = ? ORDER BY recorded_at DESC, rowid DESC LIMIT ?"
            ") ORDER BY recorded_at, rid",
            (user_id, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


# --- chat_messages ---------------------------------------------------------------------------


def _chat_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    raw = d.get("actions")
    if raw is not None:
        try:
            d["actions"] = json.loads(raw)
        except (TypeError, ValueError):
            d["actions"] = None
    return d


def add_chat_message(
    role: str,
    content: str,
    actions: dict | list | None = None,
    *,
    user_id: str = DEFAULT_USER,
    conn: Conn = None,
) -> dict[str, Any]:
    """Store a chat message. Returns ``{id, role, content, actions, created_at}``."""
    if role not in ("user", "assistant"):
        raise ValueError(f"Invalid role: {role!r}")
    msg = {
        "id": new_id(),
        "role": role,
        "content": content,
        "actions": actions,
        "created_at": now_iso(),
    }
    with use_connection(conn) as c:
        c.execute(
            "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                msg["id"],
                user_id,
                role,
                content,
                json.dumps(actions) if actions is not None else None,
                msg["created_at"],
            ),
        )
    return msg


def get_chat_messages(
    *, limit: int = 50, user_id: str = DEFAULT_USER, conn: Conn = None
) -> list[dict[str, Any]]:
    """The most recent ``limit`` messages, oldest first, with ``actions`` parsed from JSON."""
    with use_connection(conn) as c:
        rows = c.execute(
            "SELECT id, role, content, actions, created_at FROM ("
            "  SELECT *, rowid AS rid FROM chat_messages WHERE user_id = ?"
            "  ORDER BY created_at DESC, rowid DESC LIMIT ?"
            ") ORDER BY created_at, rid",
            (user_id, int(limit)),
        ).fetchall()
    return [_chat_row(r) for r in rows]
