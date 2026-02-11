"""Tests for chat endpoint."""



class TestChat:
    async def test_send_message(self, client):
        resp = await client.post("/api/chat", json={"message": "Hello"})
        assert resp.status_code == 200
        data = resp.json()
        assert "message" in data
        assert data["trades"] == []
        assert data["watchlist_changes"] == []

    async def test_chat_history(self, client):
        await client.post("/api/chat", json={"message": "Hello"})
        resp = await client.get("/api/chat/history")
        assert resp.status_code == 200
        messages = resp.json()["messages"]
        assert len(messages) == 2  # user + assistant
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello"
        assert messages[1]["role"] == "assistant"
