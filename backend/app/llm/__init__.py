"""LLM chat integration module.

Public API:
    process_chat_message - Core chat orchestrator
    ChatRequest          - Incoming chat request schema
    ChatResponse         - Outgoing chat response schema
"""

from .models import ChatRequest, ChatResponse
from .service import process_chat_message

__all__ = ["process_chat_message", "ChatRequest", "ChatResponse"]
