"""Portfolio test fixtures: temp DB + a pre-loaded PriceCache."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import db
from app.market import PriceCache


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    db_path = str(tmp_path / "portfolio.db")
    monkeypatch.setenv("FINALLY_DB_PATH", db_path)
    db.reset_initialization_state()
    db.ensure_initialized()
    yield db_path
    db.reset_initialization_state()
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
def cache_with_prices() -> PriceCache:
    cache = PriceCache()
    cache.update("AAPL", 100.0)
    cache.update("GOOGL", 200.0)
    cache.update("TSLA", 250.0)
    return cache
