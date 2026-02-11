"""Unit tests for LLM service layer."""

import json

from app.llm.mock import get_mock_response
from app.llm.service import (
    load_chat_history,
    parse_llm_response,
    process_chat_message,
    save_chat_message,
)

# --- Parsing tests ---


def test_parse_llm_response_valid_json():
    """Valid JSON with message + trades + watchlist_changes parses correctly."""
    data = {
        "message": "Here you go",
        "trades": [{"ticker": "AAPL", "side": "buy", "quantity": 10}],
        "watchlist_changes": [{"ticker": "PYPL", "action": "add"}],
    }
    result = parse_llm_response(json.dumps(data))
    assert result.message == "Here you go"
    assert len(result.trades) == 1
    assert result.trades[0].ticker == "AAPL"
    assert result.trades[0].side == "buy"
    assert result.trades[0].quantity == 10
    assert len(result.watchlist_changes) == 1
    assert result.watchlist_changes[0].ticker == "PYPL"
    assert result.watchlist_changes[0].action == "add"


def test_parse_llm_response_message_only():
    """JSON with only message field defaults to empty action lists."""
    data = {"message": "Just a message"}
    result = parse_llm_response(json.dumps(data))
    assert result.message == "Just a message"
    assert result.trades == []
    assert result.watchlist_changes == []


def test_parse_llm_response_invalid_json_fallback():
    """Non-JSON string falls back to message-only response."""
    result = parse_llm_response("This is not JSON at all")
    assert result.message == "This is not JSON at all"
    assert result.trades == []
    assert result.watchlist_changes == []


def test_parse_llm_response_malformed_json_fallback():
    """Malformed JSON string falls back to message-only response."""
    result = parse_llm_response('{"message": "broken')
    assert result.message == '{"message": "broken'
    assert result.trades == []
    assert result.watchlist_changes == []


# --- Mock mode tests ---


def test_mock_response_buy_keyword():
    """Message containing 'buy' returns response with buy trade action."""
    response = json.loads(get_mock_response("please buy AAPL"))
    assert len(response["trades"]) == 1
    assert response["trades"][0]["side"] == "buy"
    assert response["trades"][0]["ticker"] == "AAPL"


def test_mock_response_sell_keyword():
    """Message containing 'sell' returns response with sell trade action."""
    response = json.loads(get_mock_response("sell my AAPL"))
    assert len(response["trades"]) == 1
    assert response["trades"][0]["side"] == "sell"


def test_mock_response_watchlist_keyword():
    """Message containing 'add' returns watchlist change."""
    response = json.loads(get_mock_response("add PYPL to watchlist"))
    assert len(response["watchlist_changes"]) == 1
    assert response["watchlist_changes"][0]["ticker"] == "PYPL"
    assert response["watchlist_changes"][0]["action"] == "add"


def test_mock_response_default():
    """Generic message returns portfolio-aware message, no actions."""
    response = json.loads(get_mock_response("hello there"))
    assert "portfolio" in response["message"].lower()
    assert response["trades"] == []
    assert response["watchlist_changes"] == []


# --- Chat history tests ---


async def test_save_and_load_chat_history(db):
    """Save 3 messages, load with default limit, verify chronological order."""
    await save_chat_message(db, "user", "first message")
    await save_chat_message(db, "assistant", "first reply")
    await save_chat_message(db, "user", "second message")

    history = await load_chat_history(db)
    assert len(history) == 3
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "first message"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "first reply"
    assert history[2]["role"] == "user"
    assert history[2]["content"] == "second message"


async def test_load_chat_history_limit(db):
    """Save 5 messages, load with limit=2, verify only latest 2 returned."""
    for i in range(5):
        await save_chat_message(db, "user", f"message {i}")

    history = await load_chat_history(db, limit=2)
    assert len(history) == 2
    assert history[0]["content"] == "message 3"
    assert history[1]["content"] == "message 4"


async def test_load_chat_history_empty(db):
    """Load from empty DB returns empty list."""
    history = await load_chat_history(db)
    assert history == []


# --- Full process_chat_message tests (all with LLM_MOCK=true) ---


async def test_process_chat_default_message(db, price_cache, market_source, monkeypatch):
    """Generic message returns response with message, no actions."""
    monkeypatch.setenv("LLM_MOCK", "true")
    result = await process_chat_message(db, price_cache, market_source, "hello")
    assert result.message
    assert result.trades == []
    assert result.watchlist_changes == []


async def test_process_chat_buy_executes_trade(db, price_cache, market_source, monkeypatch):
    """'buy' message executes trade: AAPL bought, cash decreased."""
    monkeypatch.setenv("LLM_MOCK", "true")
    result = await process_chat_message(db, price_cache, market_source, "buy some stock")

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.status == "executed"
    assert trade.ticker == "AAPL"
    assert trade.side == "buy"
    assert trade.quantity == 5
    assert trade.price == 150.00
    assert trade.total == 750.00

    # Verify cash decreased
    rows = await db.execute_fetchall(
        "SELECT cash_balance FROM users_profile WHERE id = 'default'"
    )
    assert rows[0][0] == 10000.0 - 750.0


async def test_process_chat_sell_without_position_reports_failure(
    db, price_cache, market_source, monkeypatch
):
    """'sell' without position reports failure, not exception."""
    monkeypatch.setenv("LLM_MOCK", "true")
    result = await process_chat_message(db, price_cache, market_source, "sell my stock")

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.status == "failed"
    assert trade.ticker == "AAPL"
    assert "Insufficient shares" in trade.error


async def test_process_chat_watchlist_add(db, price_cache, market_source, monkeypatch):
    """'add to watchlist' message adds ticker, updates market source."""
    monkeypatch.setenv("LLM_MOCK", "true")
    result = await process_chat_message(
        db, price_cache, market_source, "add PYPL to my watchlist"
    )

    assert len(result.watchlist_changes) == 1
    change = result.watchlist_changes[0]
    assert change.status == "applied"
    assert change.ticker == "PYPL"
    assert change.action == "add"

    # Verify PYPL in watchlist table
    rows = await db.execute_fetchall(
        "SELECT ticker FROM watchlist WHERE ticker = 'PYPL' AND user_id = 'default'"
    )
    assert len(rows) == 1

    # Verify market_source.add_ticker was called
    assert "PYPL" in market_source.added_tickers


async def test_process_chat_messages_persisted(db, price_cache, market_source, monkeypatch):
    """Messages (user + assistant) persist in chat_messages table."""
    monkeypatch.setenv("LLM_MOCK", "true")
    await process_chat_message(db, price_cache, market_source, "hello there")

    rows = await db.execute_fetchall(
        "SELECT role, content FROM chat_messages WHERE user_id = 'default' ORDER BY created_at"
    )
    assert len(rows) == 2
    assert rows[0][0] == "user"
    assert rows[0][1] == "hello there"
    assert rows[1][0] == "assistant"
    assert rows[1][1]  # Non-empty assistant message


async def test_process_chat_history_included(db, price_cache, market_source, monkeypatch):
    """Prior messages in DB are loaded; function completes without error."""
    monkeypatch.setenv("LLM_MOCK", "true")
    # Save a prior message
    await save_chat_message(db, "user", "previous question")
    await save_chat_message(db, "assistant", "previous answer")

    # Should not crash -- history is loaded and used in messages array
    result = await process_chat_message(db, price_cache, market_source, "follow up")
    assert result.message


async def test_process_chat_snapshot_after_trade(db, price_cache, market_source, monkeypatch):
    """Trade execution triggers a portfolio snapshot recording."""
    monkeypatch.setenv("LLM_MOCK", "true")
    await process_chat_message(db, price_cache, market_source, "buy AAPL")

    rows = await db.execute_fetchall(
        "SELECT total_value FROM portfolio_snapshots WHERE user_id = 'default'"
    )
    assert len(rows) >= 1
