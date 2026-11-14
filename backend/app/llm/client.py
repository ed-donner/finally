"""LiteLLM -> OpenRouter -> Cerebras client (see .claude/skills/cerebras/SKILL.md)."""

from __future__ import annotations

import os
from typing import Any

from .schemas import ChatLLMResponse

MODEL = "openrouter/openai/gpt-oss-120b"
EXTRA_BODY = {"provider": {"order": ["cerebras"]}}
TIMEOUT_SECONDS = 60


class LLMNotConfiguredError(RuntimeError):
    """OPENROUTER_API_KEY is missing."""


def is_configured() -> bool:
    return bool(os.getenv("OPENROUTER_API_KEY", "").strip())


async def _acompletion(**kwargs: Any) -> Any:
    """Thin wrapper so tests can monkeypatch the network call. litellm is imported lazily
    (it is slow to import and not needed in mock mode)."""
    from litellm import acompletion

    return await acompletion(**kwargs)


async def call_llm(messages: list[dict[str, str]]) -> str:
    """Call the model with structured output; returns the raw content string."""
    if not is_configured():
        raise LLMNotConfiguredError("OPENROUTER_API_KEY is not set")
    response = await _acompletion(
        model=MODEL,
        messages=messages,
        response_format=ChatLLMResponse,
        reasoning_effort="low",
        extra_body=EXTRA_BODY,
        timeout=TIMEOUT_SECONDS,
    )
    return response.choices[0].message.content or ""
