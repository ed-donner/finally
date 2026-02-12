"""SQLite helpers and lazy schema initialization for FinAlly."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from uuid import uuid4

DEFAULT_USER_ID = "default"
DEFAULT_CASH_BALANCE = 10000.0
DEFAULT_WATCHLIST = [
    "AAPL",
    "GOOGL",
    "MSFT",
    "AMZN",
    "TSLA",
    "NVDA",
    "META",
    "JPM",
    "V",
    "NFLX",
]

_init_lock = Lock()
_initialized_paths: set[Path] = set()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def get_db_path() -> Path:
    import os

    configured = os.environ.get("FINALLY_DB_PATH")
    if configured:
        return Path(configured)
    root = Path(__file__).resolve().parents[2]
    return root / "db" / "finally.db"


def get_connection() -> sqlite3.Connection:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_db_initialized() -> None:
    db_path = get_db_path().resolve()
    with _init_lock:
        if db_path in _initialized_paths:
            return

        db_path.parent.mkdir(parents=True, exist_ok=True)
        with get_connection() as conn:
            _create_schema(conn)
            _seed_defaults(conn)
            conn.commit()

        _initialized_paths.add(db_path)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users_profile (
            id TEXT PRIMARY KEY,
            cash_balance REAL NOT NULL DEFAULT 10000.0,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            added_at TEXT NOT NULL,
            UNIQUE(user_id, ticker)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            quantity REAL NOT NULL,
            avg_cost REAL NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, ticker)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            executed_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            total_value REAL NOT NULL,
            recorded_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            actions TEXT,
            created_at TEXT NOT NULL
        )
        """
    )


def _seed_defaults(conn: sqlite3.Connection) -> None:
    profile = conn.execute(
        "SELECT id FROM users_profile WHERE id = ?",
        (DEFAULT_USER_ID,),
    ).fetchone()
    if profile is None:
        conn.execute(
            "INSERT INTO users_profile (id, cash_balance, created_at) VALUES (?, ?, ?)",
            (DEFAULT_USER_ID, DEFAULT_CASH_BALANCE, now_iso()),
        )

    watchlist_count = conn.execute(
        "SELECT COUNT(*) AS count FROM watchlist WHERE user_id = ?",
        (DEFAULT_USER_ID,),
    ).fetchone()["count"]
    if watchlist_count == 0:
        for ticker in DEFAULT_WATCHLIST:
            conn.execute(
                "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
                (str(uuid4()), DEFAULT_USER_ID, ticker, now_iso()),
            )


def encode_actions(actions: dict) -> str:
    return json.dumps(actions, separators=(",", ":"))
