"""SQLite connection management."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = "db/finally.db"
DB_PATH_ENV = "FINALLY_DB_PATH"


def get_db_path() -> str:
    """Resolve the SQLite file path. Honors $FINALLY_DB_PATH, else 'db/finally.db'."""
    return os.environ.get(DB_PATH_ENV, DEFAULT_DB_PATH)


def _connect(path: str) -> sqlite3.Connection:
    """Open a connection with WAL + foreign keys enabled. Creates parent dirs as needed."""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def connect(path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a connection scoped to a single operation. Closes on exit."""
    conn = _connect(path or get_db_path())
    try:
        yield conn
    finally:
        conn.close()
