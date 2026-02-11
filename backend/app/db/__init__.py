"""Database layer for FinAlly.

Public API:
    init_db   - Initialize database with schema and seed data
    close_db  - Close the database connection
"""

from .connection import close_db, init_db

__all__ = ["init_db", "close_db"]
