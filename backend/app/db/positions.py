"""Positions repository."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from .connection import connect
from .init import DEFAULT_USER_ID


@dataclass(frozen=True)
class Position:
    ticker: str
    quantity: float
    avg_cost: float
    updated_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def list_positions(user_id: str = DEFAULT_USER_ID) -> list[Position]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT ticker, quantity, avg_cost, updated_at FROM positions "
            "WHERE user_id = ? AND quantity > 0 ORDER BY ticker ASC",
            (user_id,),
        ).fetchall()
        return [
            Position(
                ticker=r["ticker"],
                quantity=float(r["quantity"]),
                avg_cost=float(r["avg_cost"]),
                updated_at=r["updated_at"],
            )
            for r in rows
        ]


def get_position(ticker: str, user_id: str = DEFAULT_USER_ID) -> Position | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT ticker, quantity, avg_cost, updated_at FROM positions "
            "WHERE user_id = ? AND ticker = ?",
            (user_id, ticker.upper()),
        ).fetchone()
        if row is None:
            return None
        return Position(
            ticker=row["ticker"],
            quantity=float(row["quantity"]),
            avg_cost=float(row["avg_cost"]),
            updated_at=row["updated_at"],
        )


def upsert_position(
    ticker: str, quantity: float, avg_cost: float, user_id: str = DEFAULT_USER_ID
) -> None:
    """Insert or update a position. Uses ON CONFLICT for idempotent upsert."""
    ticker = ticker.upper()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, ticker) DO UPDATE SET
                quantity = excluded.quantity,
                avg_cost = excluded.avg_cost,
                updated_at = excluded.updated_at
            """,
            (str(uuid.uuid4()), user_id, ticker, quantity, avg_cost, _now()),
        )


def delete_position(ticker: str, user_id: str = DEFAULT_USER_ID) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM positions WHERE user_id = ? AND ticker = ?",
            (user_id, ticker.upper()),
        )
