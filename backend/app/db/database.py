"""SQLite database initialization and connection management."""

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[3] / "db" / "finally.db"

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]

_connection: sqlite3.Connection | None = None


def _create_connection(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _needs_init(conn: sqlite3.Connection) -> bool:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users_profile'"
    )
    return cursor.fetchone() is None


def _seed(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO users_profile (id, cash_balance, created_at) VALUES (?, ?, ?)",
        ("default", 10000.0, now),
    )
    for ticker in DEFAULT_TICKERS:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), "default", ticker, now),
        )
    conn.commit()


def init_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Initialize the database: create tables and seed if needed."""
    global _connection
    conn = _create_connection(db_path)
    if _needs_init(conn):
        schema = SCHEMA_PATH.read_text()
        conn.executescript(schema)
        _seed(conn)
    _connection = conn
    return conn


def get_db() -> sqlite3.Connection:
    """Get the database connection, initializing if needed."""
    global _connection
    if _connection is None:
        return init_db()
    return _connection
