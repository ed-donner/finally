"""HTTP route tests for /api/chat (mock LLM mode)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app


@pytest.fixture
def chat_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Like the standard `client` fixture but with LLM_MOCK=true forced on."""
    db_path = str(tmp_path / "chat.db")
    monkeypatch.setenv("FINALLY_DB_PATH", db_path)
    monkeypatch.setenv("LLM_MOCK", "true")
    db.reset_initialization_state()
    db.ensure_initialized()

    app = create_app()
    with TestClient(app) as c:
        cache = app.state.finally_state.price_cache
        import time
        deadline = time.time() + 3.0
        while len(cache) < 5 and time.time() < deadline:
            time.sleep(0.05)
        yield c

    db.reset_initialization_state()
    if os.path.exists(db_path):
        os.remove(db_path)


def test_history_initially_empty(chat_client: TestClient) -> None:
    r = chat_client.get("/api/chat")
    assert r.status_code == 200
    assert r.json() == []


def test_post_returns_user_and_assistant(chat_client: TestClient) -> None:
    r = chat_client.post("/api/chat", json={"message": "hello"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_message"]["content"] == "hello"
    assert body["assistant_message"]["role"] == "assistant"
    assert body["assistant_message"]["actions"] is None  # no trades for "hello"


def test_post_with_trade_requires_approval_by_default(chat_client: TestClient) -> None:
    r = chat_client.post("/api/chat", json={"message": "buy 1 AAPL"})
    assert r.status_code == 200
    actions = r.json()["assistant_message"]["actions"]
    assert actions is not None
    assert actions["trades"][0]["status"] == "approval_required"
    assert db.positions.get_position("AAPL") is None


def test_post_with_trade_executes_when_approved(chat_client: TestClient) -> None:
    r = chat_client.post(
        "/api/chat",
        json={"message": "buy 1 AAPL", "allow_trade_execution": True},
    )
    assert r.status_code == 200
    actions = r.json()["assistant_message"]["actions"]
    assert actions is not None
    assert actions["trades"][0]["status"] == "executed"
    assert actions["trades"][0]["ticker"] == "AAPL"


def test_history_returned_after_messages(chat_client: TestClient) -> None:
    chat_client.post("/api/chat", json={"message": "hi"})
    chat_client.post("/api/chat", json={"message": "buy 1 AAPL"})
    r = chat_client.get("/api/chat")
    history = r.json()
    # 2 user + 2 assistant
    assert len(history) == 4
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]


def test_empty_message_422(chat_client: TestClient) -> None:
    r = chat_client.post("/api/chat", json={"message": ""})
    assert r.status_code == 422


def test_history_limit_validation(chat_client: TestClient) -> None:
    assert chat_client.get("/api/chat?limit=0").status_code == 400
    assert chat_client.get("/api/chat?limit=501").status_code == 400
