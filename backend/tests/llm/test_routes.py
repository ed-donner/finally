"""POST /api/chat and GET /api/chat/history."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.llm.mock import MOCK_DEFAULT_MESSAGE
from app.routes.chat import router


@pytest.fixture
def client(mock_mode, price_cache, source):
    app = FastAPI()
    app.include_router(router)
    app.state.price_cache = price_cache
    app.state.market_source = source
    return TestClient(app)


def test_post_chat_default(client):
    r = client.post("/api/chat", json={"message": "hello"})
    assert r.status_code == 200
    assert r.json() == {"message": MOCK_DEFAULT_MESSAGE, "trades": [], "watchlist_changes": []}


def test_post_chat_trade(client):
    body = client.post("/api/chat", json={"message": "buy 1 AAPL"}).json()
    assert body["trades"][0]["status"] == "executed"
    assert set(body["trades"][0]) == {"ticker", "side", "quantity", "status", "price", "error"}


@pytest.mark.parametrize("msg", ["", "   "])
def test_post_chat_empty_400(client, msg):
    r = client.post("/api/chat", json={"message": msg})
    assert r.status_code == 400 and "detail" in r.json()


def test_history(client):
    assert client.get("/api/chat/history").json() == {"messages": []}
    client.post("/api/chat", json={"message": "add PYPL"})
    msgs = client.get("/api/chat/history").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert set(msgs[1]) == {"id", "role", "content", "actions", "created_at"}
    assert msgs[1]["actions"]["watchlist_changes"][0]["ticker"] == "PYPL"
