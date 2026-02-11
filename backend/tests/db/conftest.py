"""Fixtures for database tests."""

import pytest

from app.db import init_db


@pytest.fixture
async def db(tmp_path):
    """Create an isolated database per test."""
    db_path = str(tmp_path / "test.db")
    conn = await init_db(db_path)
    yield conn
    await conn.close()


@pytest.fixture
def db_path(tmp_path):
    """Return a tmp path string for tests that manage their own connections."""
    return str(tmp_path / "test.db")
