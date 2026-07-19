"""Fixtures for API tests using TestClient (lifespan bypassed)."""

import pytest
from fastapi.testclient import TestClient

from app import runtime
from app.db import init_db
from app.db.connection import _reset_for_tests
from app.main import create_app
from tests.support import FakeSource, seeded_cache


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient with a fresh DB and deterministic prices.

    Constructed WITHOUT the context-manager form so the real lifespan (which
    would start the live simulator) does not run; runtime is wired manually.
    Yields (client, cache).
    """
    monkeypatch.setenv("LLM_MOCK", "true")
    _reset_for_tests()
    init_db(str(tmp_path / "test.db"))
    cache = seeded_cache()
    source = FakeSource(cache)
    runtime.set_runtime(cache, source)
    app = create_app()
    yield TestClient(app), cache
    runtime.reset_runtime()
    _reset_for_tests()
