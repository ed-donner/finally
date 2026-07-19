"""SQLite connection handling and lazy schema initialization.

Path resolution: the active database path comes from (in priority order)
the ``db_path`` argument to :func:`init_db`, the ``FINALLY_DB_PATH`` env var,
or the default ``db/finally.db`` under the repo root (the Docker volume mount).

Thread safety: connections are cached per-thread via :class:`threading.local`,
so each thread gets its own ``sqlite3.Connection``. Every connection uses
``sqlite3.Row`` as its row factory.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

# app/db/connection.py -> parents: [0]=db, [1]=app, [2]=backend, [3]=repo root
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCHEMA_PATH = _BACKEND_DIR / "db" / "schema.sql"
_DEFAULT_DB_PATH = "db/finally.db"

# Default seed data (PLAN §7).
_DEFAULT_USER_ID = "default"
_DEFAULT_CASH = 10000.0
_DEFAULT_TICKERS = [
    "AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
    "NVDA", "META", "JPM", "V", "NFLX",
]

_state = threading.local()
_init_lock = threading.Lock()
_db_path: str | None = None
_initialized: set[str] = set()


def _resolve_db_path(db_path: str | None = None) -> str:
    """Resolve the DB path from arg > env > default, as an absolute path string."""
    raw = db_path or os.environ.get("FINALLY_DB_PATH") or _DEFAULT_DB_PATH
    path = Path(raw)
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return str(path)


def _current_path() -> str:
    """The path init_db selected, or the resolved default if init hasn't run."""
    return _db_path if _db_path is not None else _resolve_db_path()


def get_conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection for the active database path.

    Each thread caches its own connection keyed by path, so connections are
    never shared across threads. Rows come back as ``sqlite3.Row``.
    """
    path = _current_path()
    conns: dict[str, sqlite3.Connection] | None = getattr(_state, "conns", None)
    if conns is None:
        conns = {}
        _state.conns = conns

    conn = conns.get(path)
    if conn is None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conns[path] = conn
    return conn


def init_db(db_path: str | None = None) -> None:
    """Create the schema and seed default data if the database is empty.

    Idempotent: safe to call repeatedly. Selects the active DB path (see module
    docstring), records it for subsequent :func:`get_conn` calls, applies the
    schema, and seeds the default profile ($10k) plus the 10 default tickers
    only when they are missing.
    """
    global _db_path
    path = _resolve_db_path(db_path)

    with _init_lock:
        _db_path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = get_conn()
        _apply_schema(conn)
        _seed(conn)
        _initialized.add(path)


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Execute schema.sql (all statements use IF NOT EXISTS)."""
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    conn.commit()


def _seed(conn: sqlite3.Connection) -> None:
    """Seed the default profile and watchlist, only when they are absent."""
    from datetime import datetime, timezone
    from uuid import uuid4

    now = datetime.now(timezone.utc).isoformat()

    profile = conn.execute(
        "SELECT 1 FROM users_profile WHERE id = ?", (_DEFAULT_USER_ID,)
    ).fetchone()
    if profile is None:
        conn.execute(
            "INSERT INTO users_profile (id, cash_balance, created_at) VALUES (?, ?, ?)",
            (_DEFAULT_USER_ID, _DEFAULT_CASH, now),
        )

    watch_count = conn.execute(
        "SELECT COUNT(*) AS n FROM watchlist WHERE user_id = ?", (_DEFAULT_USER_ID,)
    ).fetchone()["n"]
    if watch_count == 0:
        conn.executemany(
            "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
            [(uuid4().hex, _DEFAULT_USER_ID, ticker, now) for ticker in _DEFAULT_TICKERS],
        )

    conn.commit()


def _reset_for_tests() -> None:
    """Test helper: clear cached path/connections so a fresh init_db can run."""
    global _db_path
    conns: dict[str, sqlite3.Connection] | None = getattr(_state, "conns", None)
    if conns:
        for conn in conns.values():
            conn.close()
        conns.clear()
    _db_path = None
    _initialized.clear()
