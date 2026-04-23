"""Shared DB test fixtures: each test gets an initialized temp database."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import db


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point FINALLY_DB_PATH at a fresh file for the duration of the test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("FINALLY_DB_PATH", db_path)
    db.reset_initialization_state()
    db.ensure_initialized()
    yield db_path
    db.reset_initialization_state()
    if os.path.exists(db_path):
        os.remove(db_path)
