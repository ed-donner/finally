"""Database connection management."""

import os
from pathlib import Path

import aiosqlite

# Default path: db/finally.db relative to project root (one level above backend/)
_db_path: str | None = None


def get_db_path() -> str:
    """Return the configured database path, or the default."""
    if _db_path is not None:
        return _db_path
    return os.environ.get("FINALLY_DB_PATH", str(Path(__file__).resolve().parents[3] / "db" / "finally.db"))


def set_db_path(path: str | None) -> None:
    """Override the database path. Pass None to reset to default."""
    global _db_path
    _db_path = path


async def get_connection() -> aiosqlite.Connection:
    """Open a new async SQLite connection with WAL mode and foreign keys enabled."""
    db = await aiosqlite.connect(get_db_path())
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db
