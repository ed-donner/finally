from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

# backend/db/connection.py -> project root is two levels up from backend/.
DB_PATH = Path(__file__).resolve().parent.parent.parent / "db" / "finally.db"

DEFAULT_USER_ID = "default"
DEFAULT_WATCHLIST = [
    "AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
    "NVDA", "META", "JPM", "V", "NFLX",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users_profile (
    id TEXT PRIMARY KEY,
    cash_balance REAL NOT NULL DEFAULT 10000.0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    ticker TEXT NOT NULL,
    added_at TEXT NOT NULL,
    UNIQUE (user_id, ticker)
);

CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    ticker TEXT NOT NULL,
    quantity REAL NOT NULL,
    avg_cost REAL NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, ticker)
);
"""


_initialized_paths: set[Path] = set()


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)

    (count,) = conn.execute("SELECT COUNT(*) FROM users_profile").fetchone()
    if count == 0:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO users_profile (id, cash_balance, created_at) VALUES (?, ?, ?)",
            (DEFAULT_USER_ID, 10000.0, now),
        )
        conn.executemany(
            "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
            [(str(uuid.uuid4()), DEFAULT_USER_ID, ticker, now) for ticker in DEFAULT_WATCHLIST],
        )
        conn.commit()


def get_connection() -> sqlite3.Connection:
    """Open a connection to the SQLite database, lazily creating and seeding
    the schema the first time this path is seen. Callers own the returned
    connection and must close it."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if DB_PATH not in _initialized_paths:
        _init_schema(conn)
        _initialized_paths.add(DB_PATH)
    return conn
