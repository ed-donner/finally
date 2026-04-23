"""Thin wrapper around LiteLLM → OpenRouter → Cerebras with structured output.

Mock mode (LLM_MOCK=true) bypasses the network entirely — see `mock.py`.
"""

from __future__ import annotations

import logging
import os

from litellm import completion

from .mock import mock_response
from .schema import LLMResponse

logger = logging.getLogger(__name__)

MODEL = "openrouter/openai/gpt-oss-120b"
EXTRA_BODY = {"provider": {"order": ["cerebras"]}}
MOCK_ENV = "LLM_MOCK"
API_KEY_ENV = "OPENROUTER_API_KEY"


def is_mock_enabled() -> bool:
    return os.environ.get(MOCK_ENV, "").strip().lower() == "true"


def has_api_key() -> bool:
    return bool(os.environ.get(API_KEY_ENV, "").strip())


def complete_chat(messages: list[dict[str, str]], user_message: str) -> LLMResponse:
    """Call the LLM and return a validated LLMResponse.

    `messages` is the full prompt list (system + portfolio context + history + new user msg).
    `user_message` is the latest raw user input — used by mock mode for pattern matching.
    """
    if is_mock_enabled():
        logger.info("LLM_MOCK enabled — using deterministic mock response")
        return mock_response(user_message)
    if not has_api_key():
        logger.warning("%s missing — falling back to deterministic mock mode", API_KEY_ENV)
        return mock_response(user_message)

    response = completion(
        model=MODEL,
        messages=messages,
        response_format=LLMResponse,
        reasoning_effort="low",
        extra_body=EXTRA_BODY,
    )
    raw = response.choices[0].message.content
    return LLMResponse.model_validate_json(raw)
