"""SQLite connection management, lazy initialization and seeding."""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_USER = "default"
DEFAULT_CASH = 10000.0
DEFAULT_WATCHLIST = ("AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX")

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
# backend/app/db/connection.py -> parents[3] is the repo root (locally).
# In the container the package lives at /app/backend/app, so FINALLY_DB_PATH is set there.
_REPO_ROOT = Path(__file__).resolve().parents[3]

_init_lock = threading.Lock()
_initialized: set[str] = set()


def now_iso() -> str:
    """Current time as an ISO-8601 UTC string."""
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def get_db_path() -> Path:
    """Database file path: env FINALLY_DB_PATH, else <repo-root>/db/finally.db."""
    env = os.environ.get("FINALLY_DB_PATH", "").strip()
    if env:
        return Path(env)
    return _REPO_ROOT / "db" / "finally.db"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(path),
        timeout=10.0,
        isolation_level=None,  # autocommit; transaction() issues BEGIN IMMEDIATE explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(path: Path | None = None) -> None:
    """Create the database directory, tables and seed data. Idempotent.

    If ``path`` is given it is used directly (and ``FINALLY_DB_PATH`` is not consulted).
    Note that the module-level repository functions always use ``get_db_path()``, so tests
    should set ``FINALLY_DB_PATH`` to the same path.
    """
    path = Path(path) if path is not None else get_db_path()
    with _init_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(path)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA_PATH.read_text())
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO users_profile (id, cash_balance, created_at) "
                    "VALUES (?, ?, ?)",
                    (DEFAULT_USER, DEFAULT_CASH, now_iso()),
                )
                if cur.rowcount == 1:
                    # Fresh database: seed the default watchlist (only once, so a user who
                    # empties their watchlist doesn't get it back on restart).
                    for ticker in DEFAULT_WATCHLIST:
                        conn.execute(
                            "INSERT OR IGNORE INTO watchlist (id, user_id, ticker, added_at) "
                            "VALUES (?, ?, ?, ?)",
                            (new_id(), DEFAULT_USER, ticker, now_iso()),
                        )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        _initialized.add(str(path.resolve()))


def get_connection() -> sqlite3.Connection:
    """Open a new connection to the configured DB (lazily initializing it if needed).

    Connections are autocommit (``isolation_level=None``); use ``transaction()`` to group
    writes. The caller is responsible for closing the connection.
    """
    path = get_db_path()
    key = str(path.resolve())
    if key not in _initialized or not path.exists():
        _initialized.discard(key)
        init_db(path)
    return _connect(path)


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Yield a connection inside ``BEGIN IMMEDIATE``; commit on success, roll back on error."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()


@contextmanager
def use_connection(conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    """Yield ``conn`` if given (caller manages it), else a fresh autocommit connection."""
    if conn is not None:
        yield conn
        return
    own = get_connection()
    try:
        yield own
    finally:
        own.close()
