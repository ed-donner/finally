"""Tests for chat service."""

import os
import tempfile
from pathlib import Path

import pytest

from app.db.database import init_db
from app.market import PriceCache
from app.chat.service import ChatService


@pytest.fixture
def setup():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(tmp.name)
    tmp.close()
    db_path.unlink()
    conn = init_db(db_path)
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    cache.update("GOOGL", 175.00)
    os.environ["LLM_MOCK"] = "true"
    service = ChatService(conn, cache)
    yield service
    os.environ.pop("LLM_MOCK", None)


class TestChatMock:
    @pytest.mark.asyncio
    async def test_basic_message(self, setup):
        result = await setup.send_message("Hello")
        assert "message" in result
        assert len(result["message"]) > 0

    @pytest.mark.asyncio
    async def test_buy_message_executes_trade(self, setup):
        result = await setup.send_message("Buy 10 shares of AAPL")
        assert "actions" in result
        assert len(result["actions"]["trades"]) > 0
        assert result["actions"]["trades"][0]["ticker"] == "AAPL"

    @pytest.mark.asyncio
    async def test_message_stored(self, setup):
        await setup.send_message("Hello")
        history = setup.get_history()
        assert len(history) == 2  # user + assistant

    @pytest.mark.asyncio
    async def test_history_order(self, setup):
        await setup.send_message("First")
        await setup.send_message("Second")
        history = setup.get_history()
        assert history[0]["content"] == "First"
        assert history[0]["role"] == "user"
