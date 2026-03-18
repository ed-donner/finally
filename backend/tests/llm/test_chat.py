"""Tests for the real LLM chat function — mocks the litellm.completion call."""

from unittest.mock import MagicMock, patch

import pytest

from app.llm.chat import chat_with_llm
from app.llm.models import LLMResponse


SAMPLE_CONTEXT = {
    "cash_balance": 8500.00,
    "total_value": 12300.00,
    "positions": [
        {
            "ticker": "AAPL",
            "quantity": 10,
            "avg_cost": 185.0,
            "current_price": 195.0,
            "unrealized_pnl": 100.0,
            "pnl_percent": 5.4,
        }
    ],
    "watchlist": [
        {"ticker": "AAPL", "price": 195.0},
        {"ticker": "GOOGL", "price": 175.0},
    ],
}


def _make_mock_response(content: str):
    """Create a mock litellm response object."""
    choice = MagicMock()
    choice.message.content = content
    resp = MagicMock()
    resp.choices = [choice]
    return resp


@patch("app.llm.chat.completion")
async def test_successful_call(mock_completion):
    raw = '{"message": "Hello from LLM", "trades": [], "watchlist_changes": []}'
    mock_completion.return_value = _make_mock_response(raw)

    result = await chat_with_llm("hi", SAMPLE_CONTEXT, [])
    assert isinstance(result, LLMResponse)
    assert result.message == "Hello from LLM"
    assert result.trades == []


@patch("app.llm.chat.completion")
async def test_response_with_trade(mock_completion):
    raw = '{"message": "Buying AAPL", "trades": [{"ticker": "AAPL", "side": "buy", "quantity": 5}]}'
    mock_completion.return_value = _make_mock_response(raw)

    result = await chat_with_llm("buy AAPL", SAMPLE_CONTEXT, [])
    assert len(result.trades) == 1
    assert result.trades[0].ticker == "AAPL"
    assert result.trades[0].side == "buy"


@patch("app.llm.chat.completion")
async def test_response_with_watchlist_change(mock_completion):
    raw = '{"message": "Added PYPL", "watchlist_changes": [{"ticker": "PYPL", "action": "add"}]}'
    mock_completion.return_value = _make_mock_response(raw)

    result = await chat_with_llm("add PYPL", SAMPLE_CONTEXT, [])
    assert len(result.watchlist_changes) == 1
    assert result.watchlist_changes[0].ticker == "PYPL"


@patch("app.llm.chat.completion")
async def test_api_error_returns_friendly_message(mock_completion):
    mock_completion.side_effect = Exception("Connection timeout")

    result = await chat_with_llm("hi", SAMPLE_CONTEXT, [])
    assert isinstance(result, LLMResponse)
    assert "trouble connecting" in result.message
    assert result.trades == []
    assert result.watchlist_changes == []


@patch("app.llm.chat.completion")
async def test_invalid_json_returns_error(mock_completion):
    mock_completion.return_value = _make_mock_response("not valid json {{{")

    result = await chat_with_llm("hi", SAMPLE_CONTEXT, [])
    assert "trouble connecting" in result.message


@patch("app.llm.chat.completion")
async def test_conversation_history_included(mock_completion):
    raw = '{"message": "Got it"}'
    mock_completion.return_value = _make_mock_response(raw)
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]

    await chat_with_llm("what now?", SAMPLE_CONTEXT, history)

    call_args = mock_completion.call_args
    messages = call_args.kwargs["messages"]
    # system + 2 history + 1 user = 4
    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert messages[1]["content"] == "hello"
    assert messages[2]["content"] == "hi there"
    assert messages[3]["content"] == "what now?"


@patch("app.llm.chat.completion")
async def test_history_truncated_to_20(mock_completion):
    raw = '{"message": "ok"}'
    mock_completion.return_value = _make_mock_response(raw)
    history = [{"role": "user", "content": f"msg {i}"} for i in range(30)]

    await chat_with_llm("latest", SAMPLE_CONTEXT, history)

    call_args = mock_completion.call_args
    messages = call_args.kwargs["messages"]
    # system + 20 history + 1 user = 22
    assert len(messages) == 22


@patch("app.llm.chat.completion")
async def test_portfolio_context_in_system_prompt(mock_completion):
    raw = '{"message": "ok"}'
    mock_completion.return_value = _make_mock_response(raw)

    await chat_with_llm("show portfolio", SAMPLE_CONTEXT, [])

    call_args = mock_completion.call_args
    system_msg = call_args.kwargs["messages"][0]["content"]
    assert "$8,500.00" in system_msg
    assert "AAPL" in system_msg


@patch("app.llm.chat.completion")
async def test_model_and_extra_body(mock_completion):
    raw = '{"message": "ok"}'
    mock_completion.return_value = _make_mock_response(raw)

    await chat_with_llm("hi", SAMPLE_CONTEXT, [])

    call_args = mock_completion.call_args
    assert call_args.kwargs["model"] == "openrouter/openai/gpt-oss-120b"
    assert call_args.kwargs["extra_body"] == {"provider": {"order": ["cerebras"]}}
    assert call_args.kwargs["response_format"] == LLMResponse
