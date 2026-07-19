"""Fixtures for database tests — each test gets a fresh temp SQLite database."""

import pytest

from app.db import init_db
from app.db.connection import _reset_for_tests


@pytest.fixture
def db(tmp_path):
    """Initialize a fresh, seeded database at a temp path and yield the path."""
    db_path = tmp_path / "test.db"
    _reset_for_tests()
    init_db(str(db_path))
    yield db_path
    _reset_for_tests()
