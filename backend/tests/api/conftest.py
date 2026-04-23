"""HTTP test fixtures: build a TestClient against a temp DB.

Each test gets its own DB and its own app instance (lifespan starts the simulator,
seeds the cache via a few iterations, and shuts down cleanly on teardown).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    db_path = str(tmp_path / "api.db")
    monkeypatch.setenv("FINALLY_DB_PATH", db_path)
    db.reset_initialization_state()
    db.ensure_initialized()
    yield db_path
    db.reset_initialization_state()
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
def client(temp_db: str) -> Iterator[TestClient]:
    app = create_app()
    with TestClient(app) as c:
        # The simulator needs a few ticks to populate the cache before trade tests.
        cache = app.state.finally_state.price_cache
        deadline = time.time() + 3.0
        while len(cache) < 5 and time.time() < deadline:
            time.sleep(0.05)
        yield c
