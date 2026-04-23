"""Trades repository (append-only log)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .connection import connect
from .init import DEFAULT_USER_ID

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class Trade:
    id: str
    ticker: str
    side: Side
    quantity: float
    price: float
    executed_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def record_trade(
    ticker: str,
    side: Side,
    quantity: float,
    price: float,
    user_id: str = DEFAULT_USER_ID,
) -> Trade:
    trade = Trade(
        id=str(uuid.uuid4()),
        ticker=ticker.upper(),
        side=side,
        quantity=quantity,
        price=price,
        executed_at=_now(),
    )
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.id, user_id, trade.ticker, trade.side,
                trade.quantity, trade.price, trade.executed_at,
            ),
        )
    return trade


def list_recent(limit: int = 100, user_id: str = DEFAULT_USER_ID) -> list[Trade]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, ticker, side, quantity, price, executed_at FROM trades "
            "WHERE user_id = ? ORDER BY executed_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [
            Trade(
                id=r["id"],
                ticker=r["ticker"],
                side=r["side"],
                quantity=float(r["quantity"]),
                price=float(r["price"]),
                executed_at=r["executed_at"],
            )
            for r in rows
        ]
