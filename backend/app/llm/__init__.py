"""FinAlly AI chat assistant (LiteLLM -> OpenRouter -> Cerebras).

Public API:
    handle_chat(message, price_cache, source) -> dict   # POST /api/chat response shape
    get_chat_history(limit=100) -> list[dict]
    ChatLLMResponse, TradeInstruction, WatchlistChange, parse_llm_response
    mock_response, is_mock_enabled
"""

from .mock import is_mock_enabled, mock_response
from .schemas import ChatLLMResponse, TradeInstruction, WatchlistChange, parse_llm_response
from .service import get_chat_history, handle_chat

__all__ = [
    "ChatLLMResponse",
    "TradeInstruction",
    "WatchlistChange",
    "get_chat_history",
    "handle_chat",
    "is_mock_enabled",
    "mock_response",
    "parse_llm_response",
]
