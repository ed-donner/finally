"""Fixtures for API tests: temp DB, fake deterministic market source, TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from tests.services.fakes import FakeSource

PRICES = {"AAPL": 100.0, "NVDA": 500.0, "PYPL": 60.0}


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("FINALLY_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LLM_MOCK", "true")
    sources: list[FakeSource] = []

    def fake_factory(cache, initial_prices=None):
        src = FakeSource(cache, PRICES)
        sources.append(src)
        return src

    monkeypatch.setattr(app_main, "create_market_data_source", fake_factory)
    clients = []

    def _make(static_dir=None):
        monkeypatch.setenv("FINALLY_STATIC_DIR", str(static_dir or tmp_path / "no-static"))
        client = TestClient(app_main.create_app())
        client.__enter__()
        client.source = sources[-1]
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()
