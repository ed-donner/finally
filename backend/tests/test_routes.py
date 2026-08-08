import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from db import connection as connection_module

    monkeypatch.setattr(connection_module, "DB_PATH", tmp_path / "finally.db")
    with TestClient(app) as c:
        yield c


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_history_returns_bar_shape(client):
    resp = client.get("/api/history/AAPL", params={"days": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "AAPL"
    assert len(body["bars"]) == 5
    bar = body["bars"][0]
    assert set(bar) == {"t", "o", "h", "l", "c", "v"}


def test_history_uppercases_ticker(client):
    resp = client.get("/api/history/aapl", params={"days": 3})
    assert resp.status_code == 200
    assert resp.json()["ticker"] == "AAPL"
