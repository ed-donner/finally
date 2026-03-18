"""Fixtures for database tests — uses in-memory SQLite."""

import pytest

from app.db.connection import set_db_path
from app.db.init_db import init_db


@pytest.fixture(autouse=True)
async def setup_test_db(tmp_path):
    """Set up a fresh in-memory database for each test."""
    db_file = str(tmp_path / "test.db")
    set_db_path(db_file)
    await init_db()
    yield
    set_db_path(None)
