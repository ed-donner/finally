"""Database connection management for FinAlly.

Provides async SQLite connection with WAL mode and lazy initialization.
"""

import os

import aiosqlite

from .schema import create_tables
from .seed import seed_default_data


async def init_db(db_path: str) -> aiosqlite.Connection:
    """Initialize the database, creating tables and seeding data if needed.

    Returns an open aiosqlite connection configured with WAL mode,
    busy_timeout, and foreign keys enabled.
    """
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    db = await aiosqlite.connect(db_path, isolation_level=None)
    db.row_factory = aiosqlite.Row

    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA foreign_keys=ON")

    await create_tables(db)
    await seed_default_data(db)

    return db


async def close_db(db: aiosqlite.Connection) -> None:
    """Close the database connection."""
    await db.close()
