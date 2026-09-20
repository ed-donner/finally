"""SQLite access: lazy schema initialisation, a shared connection and a transaction helper."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# schema.sql lives in backend/db/, next to (not inside) the app package.
# backend/app/db.py -> parents[2] is the project root (where .env, db/ and static/ live).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
# Runtime database: <project root>/db/finally.db (the Docker volume mount target).
DEFAULT_DB_PATH = PROJECT_ROOT / "db" / "finally.db"


def resolve_db_path() -> str:
    """Database location: FINALLY_DB_PATH if set, otherwise <project root>/db/finally.db.

    ":memory:" is accepted (used by tests).
    """
    return os.environ.get("FINALLY_DB_PATH") or str(DEFAULT_DB_PATH)


class Database:
    """Single shared SQLite connection guarded by a re-entrant lock.

    The app is single-user, so one serialized connection is enough. All statements go
    through `execute`/`query`; multi-statement atomic work uses `transaction()`.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or resolve_db_path()
        # Make sure the db/ directory exists so a fresh checkout or volume just works.
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None -> autocommit; transactions are explicit via transaction().
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        # Rows behave like dicts, so callers use column names instead of tuple indexes.
        self._conn.row_factory = sqlite3.Row
        # Re-entrant: transaction() and execute() may nest within the same thread.
        self._lock = threading.RLock()
        # Nesting counter so an inner transaction() joins the outer one instead of erroring.
        self._tx_depth = 0

    def init_schema(self) -> None:
        """Create tables and seed data. Idempotent (every statement is IF NOT EXISTS / OR IGNORE)."""
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            # WAL gives better read/write concurrency; not applicable to in-memory databases.
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
                # NORMAL is crash-safe under WAL and avoids an fsync on every commit.
                self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic block. Nested use joins the outer transaction."""
        with self._lock:
            outer = self._tx_depth == 0
            if outer:
                # IMMEDIATE takes the write lock up front, so a read-then-write sequence
                # (e.g. check cash, then debit it) cannot be interleaved with another writer.
                self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield
            except BaseException:
                # Any error (including cancellation) rolls the whole block back, then propagates.
                # Inner blocks only propagate; the outermost one issues the ROLLBACK.
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                if outer:
                    self._conn.execute("COMMIT")
            finally:
                self._tx_depth -= 1

    def close(self) -> None:
        with self._lock:
            self._conn.close()
