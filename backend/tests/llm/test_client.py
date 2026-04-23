"""Live (mocked-network) LLM client path."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.llm import client
from app.llm.schema import LLMResponse


def test_mock_mode_bypasses_network(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MOCK", "true")
    with patch("app.llm.client.completion") as completion_mock:
        result = client.complete_chat([], "hello")
        completion_mock.assert_not_called()
    assert isinstance(result, LLMResponse)


def test_missing_api_key_falls_back_to_mock(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MOCK", "false")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with patch("app.llm.client.completion") as completion_mock:
        result = client.complete_chat([], "hello")
        completion_mock.assert_not_called()
    assert isinstance(result, LLMResponse)


def test_live_path_parses_structured_output(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MOCK", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    payload = {
        "message": "Bought.",
        "trades": [{"ticker": "AAPL", "side": "buy", "quantity": 1.0, "intent": "execute"}],
        "watchlist_changes": [],
    }
    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content=json.dumps(payload)))]

    with patch("app.llm.client.completion", return_value=fake_response) as completion_mock:
        result = client.complete_chat(
            messages=[{"role": "user", "content": "buy 1 AAPL"}],
            user_message="buy 1 AAPL",
        )
        completion_mock.assert_called_once()
        kwargs = completion_mock.call_args.kwargs
        assert kwargs["model"] == client.MODEL
        assert kwargs["response_format"] is LLMResponse
        assert kwargs["extra_body"] == client.EXTRA_BODY

    assert result.message == "Bought."
    assert len(result.trades) == 1
    assert result.trades[0].ticker == "AAPL"


def test_is_mock_enabled(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MOCK", "true")
    assert client.is_mock_enabled()
    monkeypatch.setenv("LLM_MOCK", "TRUE")
    assert client.is_mock_enabled()
    monkeypatch.setenv("LLM_MOCK", "false")
    assert not client.is_mock_enabled()
    monkeypatch.delenv("LLM_MOCK")
    assert not client.is_mock_enabled()
