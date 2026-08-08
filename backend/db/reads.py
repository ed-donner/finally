from __future__ import annotations

from .connection import get_connection


def load_active_tickers(user_id: str = "default") -> list[str]:
    """Union of watchlist tickers and tickers with an open position, so both
    the stream and portfolio valuation always have prices."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT ticker FROM watchlist WHERE user_id = ?
            UNION
            SELECT ticker FROM positions WHERE user_id = ? AND quantity > 0
            """,
            (user_id, user_id),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()
