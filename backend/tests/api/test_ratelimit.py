"""Rate limiting: 429 + Retry-After once the per-IP hourly budget is spent."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.ratelimit import RateLimiter


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LLM_MOCK", "true")
    monkeypatch.setenv("RATE_LIMIT_CHAT_PER_HOUR", "3")
    monkeypatch.setenv("RATE_LIMIT_TRADES_PER_HOUR", "2")
    with TestClient(create_app(":memory:", snapshot_interval=3600)) as c:
        yield c


def test_chat_limited_after_budget(client):
    for _ in range(3):
        assert client.post("/api/chat", json={"message": "hi"}).status_code == 200
    r = client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    assert r.json()["retry_after"] == int(r.headers["retry-after"])


def test_trade_limit_shared_across_paths_and_counts_failures(client):
    body = {"ticker": "AAPL", "quantity": 1, "side": "buy"}
    assert client.post("/api/trade", json=body).status_code == 200
    assert client.post("/api/portfolio/trade", json=body).status_code == 200
    r = client.post("/api/trade", json=body)
    assert r.status_code == 429
    assert "retry-after" in r.headers


def test_limits_are_per_ip_and_reads_unaffected(client):
    for _ in range(3):
        client.post("/api/chat", json={"message": "hi"})
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 429
    other = client.post(
        "/api/chat", json={"message": "hi"}, headers={"X-Forwarded-For": "203.0.113.9"}
    )
    assert other.status_code == 200
    assert client.get("/api/portfolio").status_code == 200


def test_window_slides():
    lim = RateLimiter({"b": (frozenset({"/x"}), 1)}, window=10)
    assert lim.check("/x", "ip", now=0) is None
    assert lim.check("/x", "ip", now=5) == 5
    assert lim.check("/x", "ip", now=10) is None
