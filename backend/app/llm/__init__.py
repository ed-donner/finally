"""LLM integration: structured-output chat with auto-executed trades + watchlist actions."""

from . import client, mock, prompt, schema, service
from .schema import (
    ChatActions,
    LLMResponse,
    TradeActionResult,
    TradeRequest,
    WatchlistActionResult,
    WatchlistChange,
)

__all__ = [
    "client",
    "mock",
    "prompt",
    "schema",
    "service",
    "ChatActions",
    "LLMResponse",
    "TradeActionResult",
    "TradeRequest",
    "WatchlistActionResult",
    "WatchlistChange",
]
