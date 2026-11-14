"""Fixtures for service-layer tests: temp DB, deterministic cache, fake source."""

from __future__ import annotations

import pytest

from app import db
from app.market import PriceCache

from .fakes import FakeSource


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("FINALLY_DB_PATH", str(path))
    db.init_db(path)
    return path


@pytest.fixture
def cache() -> PriceCache:
    c = PriceCache()
    c.update("AAPL", 100.0)
    c.update("NVDA", 500.0)
    return c


@pytest.fixture
def source(cache) -> FakeSource:
    return FakeSource(cache)
