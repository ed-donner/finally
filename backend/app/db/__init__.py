"""Database subsystem for FinAlly.

Public API:
    init_db   - Lazy schema creation + default seeding. Idempotent.
    get_conn  - Thread-local sqlite3.Connection (row_factory = sqlite3.Row).

Repository functions live in :mod:`app.db.repo`.
"""

from .connection import get_conn, init_db

__all__ = ["init_db", "get_conn"]
