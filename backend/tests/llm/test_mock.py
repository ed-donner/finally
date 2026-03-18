"""Tests for mock LLM mode."""

import pytest

from app.llm.mock import mock_chat_with_llm

EMPTY_CONTEXT = {"cash_balance": 10000, "positions": [], "watchlist": [], "total_value": 10000}


@pytest.fixture
def ctx():
    return EMPTY_CONTEXT


@pytest.fixture
def history():
    return []


async def test_buy_message(ctx, history):
    r = await mock_chat_with_llm("buy some TSLA", ctx, history)
    assert len(r.trades) == 1
    assert r.trades[0].side == "buy"
    assert r.trades[0].ticker == "TSLA"


async def test_sell_message(ctx, history):
    r = await mock_chat_with_llm("sell MSFT shares", ctx, history)
    assert len(r.trades) == 1
    assert r.trades[0].side == "sell"
    assert r.trades[0].ticker == "MSFT"


async def test_buy_default_ticker(ctx, history):
    r = await mock_chat_with_llm("buy some shares", ctx, history)
    assert r.trades[0].ticker == "AAPL"


async def test_sell_default_ticker(ctx, history):
    r = await mock_chat_with_llm("sell some shares", ctx, history)
    assert r.trades[0].ticker == "AAPL"


async def test_add_watchlist(ctx, history):
    r = await mock_chat_with_llm("add PYPL to watchlist", ctx, history)
    assert len(r.watchlist_changes) == 1
    assert r.watchlist_changes[0].action == "add"
    assert r.watchlist_changes[0].ticker == "PYPL"


async def test_remove_watchlist(ctx, history):
    r = await mock_chat_with_llm("remove AAPL from watchlist", ctx, history)
    assert len(r.watchlist_changes) == 1
    assert r.watchlist_changes[0].action == "remove"


async def test_generic_message(ctx, history):
    r = await mock_chat_with_llm("hello there", ctx, history)
    assert r.trades == []
    assert r.watchlist_changes == []
    assert "assistant" in r.message.lower()


async def test_no_actions_on_question(ctx, history):
    r = await mock_chat_with_llm("what is my portfolio value?", ctx, history)
    assert r.trades == []
    assert r.watchlist_changes == []
