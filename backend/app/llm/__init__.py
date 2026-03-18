"""LLM integration module. Routes to mock or real implementation based on LLM_MOCK env var."""

import os

from app.llm.models import LLMResponse, TradeAction, WatchlistChange


async def chat_with_llm(
    user_message: str,
    portfolio_context: dict,
    conversation_history: list[dict],
) -> LLMResponse:
    """Chat with the LLM. Uses mock mode when LLM_MOCK=true."""
    if os.environ.get("LLM_MOCK", "").lower() == "true":
        from app.llm.mock import mock_chat_with_llm
        return await mock_chat_with_llm(user_message, portfolio_context, conversation_history)

    from app.llm.chat import chat_with_llm as real_chat
    return await real_chat(user_message, portfolio_context, conversation_history)


__all__ = ["chat_with_llm", "LLMResponse", "TradeAction", "WatchlistChange"]
