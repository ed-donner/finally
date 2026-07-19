"""LLM client — routes to the deterministic mock or a real Cerebras call.

Real path uses LiteLLM -> OpenRouter with the Cerebras provider and structured
outputs, per the cerebras-inference skill (PLAN §9).
"""

from __future__ import annotations

import logging

from app.config import settings

from .mock import mock_response
from .prompts import build_messages
from .schema import ChatResponse

logger = logging.getLogger(__name__)

MODEL = "openrouter/openai/gpt-oss-120b"
EXTRA_BODY = {"provider": {"order": ["cerebras"]}}


def complete_chat(
    user_message: str,
    portfolio: dict,
    watchlist: list[dict],
    history: list[dict],
) -> ChatResponse:
    """Produce a structured ChatResponse for the user's message.

    Uses the deterministic mock when ``LLM_MOCK=true`` or when no API key is
    configured; otherwise calls the real model.
    """
    if settings.llm_mock or not settings.openrouter_api_key:
        return mock_response(user_message, portfolio)

    try:
        return _real_completion(user_message, portfolio, watchlist, history)
    except Exception as exc:  # pragma: no cover - network/parse failures
        logger.exception("LLM call failed; returning graceful fallback")
        return ChatResponse(
            message=(
                "I hit an error reaching the AI service. Your portfolio is unchanged. "
                f"({type(exc).__name__})"
            )
        )


def _real_completion(
    user_message: str,
    portfolio: dict,
    watchlist: list[dict],
    history: list[dict],
) -> ChatResponse:
    # Lazy import so the mock path and tests never require litellm installed.
    from litellm import completion

    messages = build_messages(user_message, portfolio, watchlist, history)
    response = completion(
        model=MODEL,
        messages=messages,
        response_format=ChatResponse,
        reasoning_effort="low",
        extra_body=EXTRA_BODY,
    )
    content = response.choices[0].message.content
    return ChatResponse.model_validate_json(content)
