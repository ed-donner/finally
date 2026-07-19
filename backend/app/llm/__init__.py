"""LLM integration for the FinAlly chat assistant.

Public surface:
    ChatResponse, LLMTrade, WatchlistChange - structured-output schema
    complete_chat  - produce a ChatResponse (real Cerebras call or deterministic mock)
"""

from .client import complete_chat
from .schema import ChatResponse, LLMTrade, WatchlistChange

__all__ = ["complete_chat", "ChatResponse", "LLMTrade", "WatchlistChange"]
