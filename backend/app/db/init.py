"""Lazy database initialization: create schema and seed defaults on first use."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from .connection import connect, get_db_path

DEFAULT_USER_ID = "default"
DEFAULT_CASH_BALANCE = 10_000.0
DEFAULT_WATCHLIST = [
    "AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
    "NVDA", "META", "JPM", "V", "NFLX",
]

_SCHEMA_FILE = Path(__file__).parent / "schema.sql"
_init_lock = Lock()
_initialized_paths: set[str] = set()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_FILE.read_text())


def _seed_defaults(conn: sqlite3.Connection) -> None:
    """One-time seed: only fires when the DB is fresh (no default user_profile row).

    Once the user has interacted with the app, we never re-seed — otherwise removed
    watchlist tickers would silently come back after a restart.
    """
    existing = conn.execute(
        "SELECT 1 FROM user_profile WHERE id = ?", (DEFAULT_USER_ID,)
    ).fetchone()
    if existing is not None:
        return

    seeded_at = _now()
    conn.execute(
        "INSERT INTO user_profile (id, cash_balance, created_at) VALUES (?, ?, ?)",
        (DEFAULT_USER_ID, DEFAULT_CASH_BALANCE, seeded_at),
    )
    for ticker in DEFAULT_WATCHLIST:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), DEFAULT_USER_ID, ticker, _now()),
        )
    conn.execute(
        "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) "
        "VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), DEFAULT_USER_ID, DEFAULT_CASH_BALANCE, seeded_at),
    )


def ensure_initialized(path: str | None = None) -> None:
    """Create schema and seed defaults if not already done for this DB path.

    Safe to call repeatedly; the first call per path does the work, subsequent calls are no-ops.
    Thread-safe via a module-level lock.
    """
    resolved = path or get_db_path()
    with _init_lock:
        if resolved in _initialized_paths:
            return
        with connect(resolved) as conn:
            _apply_schema(conn)
            _seed_defaults(conn)
        _initialized_paths.add(resolved)


def reset_initialization_state() -> None:
    """Test helper: clear the per-path initialized cache so tests can re-init."""
    with _init_lock:
        _initialized_paths.clear()
