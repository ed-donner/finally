"""Database module for FinAlly.

Public API:
    get_db       - Get a database connection (lazy init)
    init_db      - Initialize database schema and seed data
    DB_PATH      - Path to the SQLite database file
"""

from .database import DB_PATH, get_db, init_db

__all__ = ["DB_PATH", "get_db", "init_db"]
