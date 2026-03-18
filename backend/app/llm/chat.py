"""LLM chat function using LiteLLM via OpenRouter with Cerebras inference."""

import logging

from litellm import completion

from app.llm.models import LLMResponse
from app.llm.prompts import SYSTEM_PROMPT, format_portfolio_context

logger = logging.getLogger(__name__)

MODEL = "openrouter/openai/gpt-oss-120b"
EXTRA_BODY = {"provider": {"order": ["cerebras"]}}

ERROR_RESPONSE = LLMResponse(
    message="I'm having trouble connecting right now, please try again in a moment."
)


async def chat_with_llm(
    user_message: str,
    portfolio_context: dict,
    conversation_history: list[dict],
) -> LLMResponse:
    """Send a message to the LLM and return a structured response.

    Args:
        user_message: The user's chat message.
        portfolio_context: Dict with cash_balance, positions, watchlist, total_value.
        conversation_history: Recent chat messages as list of {"role": ..., "content": ...}.

    Returns:
        LLMResponse with message, optional trades, and optional watchlist_changes.
    """
    context_str = format_portfolio_context(portfolio_context)
    system_content = f"{SYSTEM_PROMPT}\n\n{context_str}"

    messages = [{"role": "system", "content": system_content}]

    for msg in conversation_history[-20:]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": user_message})

    try:
        response = completion(
            model=MODEL,
            messages=messages,
            response_format=LLMResponse,
            reasoning_effort="low",
            extra_body=EXTRA_BODY,
        )
        content = response.choices[0].message.content
        return LLMResponse.model_validate_json(content)
    except Exception:
        logger.exception("LLM API call failed")
        return ERROR_RESPONSE
