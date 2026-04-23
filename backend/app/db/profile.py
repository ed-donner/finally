"""User profile (cash balance) repository."""

from __future__ import annotations

from .connection import connect
from .init import DEFAULT_USER_ID


def get_cash_balance(user_id: str = DEFAULT_USER_ID) -> float:
    with connect() as conn:
        row = conn.execute(
            "SELECT cash_balance FROM user_profile WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"No profile for user_id={user_id!r}")
        return float(row["cash_balance"])


def set_cash_balance(amount: float, user_id: str = DEFAULT_USER_ID) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE user_profile SET cash_balance = ? WHERE id = ?", (amount, user_id)
        )
