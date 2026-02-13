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
LEGACY_DEFAULT_WATCHLIST = [
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

DEFAULT_WATCHLIST_GROUPS = [
    {
        "key": "tech",
        "label": "Tech",
        "tickers": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "ORCL", "CRM", "ADBE", "INTC"],
    },
    {
        "key": "financials",
        "label": "Financials",
        "tickers": ["JPM", "BAC", "WFC", "C", "GS", "MS", "V", "MA", "AXP", "BLK"],
    },
    {
        "key": "healthcare",
        "label": "Healthcare",
        "tickers": ["JNJ", "PFE", "MRK", "UNH", "ABBV", "LLY", "TMO", "ABT", "DHR", "BMY"],
    },
    {
        "key": "consumer",
        "label": "Consumer",
        "tickers": ["WMT", "COST", "HD", "MCD", "NKE", "SBUX", "KO", "PEP", "DIS", "NFLX"],
    },
    {
        "key": "industrials-energy",
        "label": "Industrials & Energy",
        "tickers": ["XOM", "CVX", "CAT", "DE", "BA", "GE", "RTX", "UPS", "UNP", "HON"],
    },
]

DEFAULT_WATCHLIST = [ticker for group in DEFAULT_WATCHLIST_GROUPS for ticker in group["tickers"]]
CUSTOM_GROUP_KEY = "custom"
CUSTOM_GROUP_LABEL = "Custom"
CUSTOM_GROUP_ORDER = 99

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
            group_key TEXT,
            group_label TEXT,
            group_order INTEGER NOT NULL DEFAULT 999,
            item_order INTEGER NOT NULL DEFAULT 999,
            added_at TEXT NOT NULL,
            UNIQUE(user_id, ticker)
        )
        """
    )
    _ensure_watchlist_columns(conn)

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

    rows = conn.execute(
        "SELECT ticker, group_key FROM watchlist WHERE user_id = ?",
        (DEFAULT_USER_ID,),
    ).fetchall()
    current_tickers = {row["ticker"] for row in rows}
    if not rows:
        _insert_default_watchlist(conn)
    elif _is_unchanged_legacy_default_watchlist(current_tickers):
        conn.execute("DELETE FROM watchlist WHERE user_id = ?", (DEFAULT_USER_ID,))
        _insert_default_watchlist(conn)
    elif _is_ungrouped_legacy_watchlist(rows):
        conn.execute("DELETE FROM watchlist WHERE user_id = ?", (DEFAULT_USER_ID,))
        _insert_default_watchlist(conn)


def _ensure_watchlist_columns(conn: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(watchlist)").fetchall()
    }
    if "group_key" not in existing_columns:
        conn.execute("ALTER TABLE watchlist ADD COLUMN group_key TEXT")
    if "group_label" not in existing_columns:
        conn.execute("ALTER TABLE watchlist ADD COLUMN group_label TEXT")
    if "group_order" not in existing_columns:
        conn.execute("ALTER TABLE watchlist ADD COLUMN group_order INTEGER NOT NULL DEFAULT 999")
    if "item_order" not in existing_columns:
        conn.execute("ALTER TABLE watchlist ADD COLUMN item_order INTEGER NOT NULL DEFAULT 999")


def _insert_default_watchlist(conn: sqlite3.Connection) -> None:
    added_at = now_iso()
    for group_index, group in enumerate(DEFAULT_WATCHLIST_GROUPS):
        for item_index, ticker in enumerate(group["tickers"]):
            conn.execute(
                "INSERT INTO watchlist (id, user_id, ticker, group_key, group_label, group_order, item_order, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    DEFAULT_USER_ID,
                    ticker,
                    group["key"],
                    group["label"],
                    group_index,
                    item_index,
                    added_at,
                ),
            )


def _is_unchanged_legacy_default_watchlist(current_tickers: set[str]) -> bool:
    legacy_tickers = set(LEGACY_DEFAULT_WATCHLIST)
    return current_tickers == legacy_tickers


def _is_ungrouped_legacy_watchlist(rows: list[sqlite3.Row]) -> bool:
    return bool(rows) and all(not row["group_key"] for row in rows)


def encode_actions(actions: dict) -> str:
    return json.dumps(actions, separators=(",", ":"))
