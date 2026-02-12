"""LLM integration for FinAlly chat actions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Literal

from litellm import completion
from pydantic import BaseModel, ConfigDict, Field, ValidationError

CHAT_MODEL = "openrouter/openai/gpt-oss-120b"
EXTRA_BODY = {"provider": {"order": ["Cerebras"]}}
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ChatLLMResponse:
    message: str
    trades: list[dict]
    watchlist_changes: list[dict]


class TradeAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0)


class WatchlistAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str
    action: Literal["add", "remove"]


class StructuredChatOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str
    trades: list[TradeAction] = []
    watchlist_changes: list[WatchlistAction] = []


def _normalize_response(payload: dict) -> ChatLLMResponse:
    message = str(payload.get("message", "")).strip() or "I reviewed your account context."

    trades: list[dict] = []
    for trade in payload.get("trades", []):
        ticker = str(trade.get("ticker", "")).upper().strip()
        side = str(trade.get("side", "")).lower().strip()
        quantity = trade.get("quantity")
        if ticker and side in {"buy", "sell"} and isinstance(quantity, (float, int)) and quantity > 0:
            trades.append({"ticker": ticker, "side": side, "quantity": float(quantity)})

    watchlist_changes: list[dict] = []
    for change in payload.get("watchlist_changes", []):
        ticker = str(change.get("ticker", "")).upper().strip()
        action = str(change.get("action", "")).lower().strip()
        if ticker and action in {"add", "remove"}:
            watchlist_changes.append({"ticker": ticker, "action": action})

    return ChatLLMResponse(
        message=message,
        trades=trades,
        watchlist_changes=watchlist_changes,
    )


def _mock_response(user_message: str) -> ChatLLMResponse:
    normalized = user_message.upper()
    trades: list[dict] = []
    watchlist_changes: list[dict] = []

    trade_pattern = re.compile(r"\b(BUY|SELL)\s+(\d+(?:\.\d+)?)\s+([A-Z]{1,10})\b")
    for side, quantity, ticker in trade_pattern.findall(normalized):
        trades.append(
            {
                "ticker": ticker,
                "side": side.lower(),
                "quantity": float(quantity),
            }
        )

    add_pattern = re.compile(r"\bADD\s+([A-Z]{1,10})\b")
    for ticker in add_pattern.findall(normalized):
        watchlist_changes.append({"ticker": ticker, "action": "add"})

    remove_pattern = re.compile(r"\bREMOVE\s+([A-Z]{1,10})\b")
    for ticker in remove_pattern.findall(normalized):
        watchlist_changes.append({"ticker": ticker, "action": "remove"})

    msg = "Mock response: I reviewed your portfolio context."
    if trades or watchlist_changes:
        msg = "Mock response: I prepared actions based on your request."

    return ChatLLMResponse(message=msg, trades=trades, watchlist_changes=watchlist_changes)


async def generate_chat_response(
    user_message: str,
    history: list[dict],
    context: dict,
) -> ChatLLMResponse:
    if os.environ.get("LLM_MOCK", "false").lower() == "true":
        return _mock_response(user_message)

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return ChatLLMResponse(
            message="LLM is unavailable because OPENROUTER_API_KEY is not configured.",
            trades=[],
            watchlist_changes=[],
        )

    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "You are FinAlly, an AI trading assistant. Be concise and data-driven. "
                "Use the provided account context and conversation history. "
                "Output strictly valid JSON using the required schema."
            ),
        },
        {
            "role": "system",
            "content": f"Account context JSON:\n{json.dumps(context, separators=(",", ":"))}",
        },
    ]

    for item in history:
        role = item.get("role", "user")
        if role not in {"user", "assistant"}:
            continue
        messages.append({"role": role, "content": str(item.get("content", ""))})

    messages.append({"role": "user", "content": user_message})

    try:
        response = await asyncio.to_thread(
            completion,
            model=CHAT_MODEL,
            api_key=api_key,
            messages=messages,
            response_format=StructuredChatOutput,
            reasoning_effort="low",
            extra_body=EXTRA_BODY,
        )
    except Exception as exc:
        logger.warning("LiteLLM/OpenRouter request failed: %s", exc)
        return ChatLLMResponse(
            message="LLM request failed. Please verify your OPENROUTER_API_KEY and try again.",
            trades=[],
            watchlist_changes=[],
        )

    try:
        content = response.choices[0].message.content or ""
        parsed_obj = StructuredChatOutput.model_validate_json(content)
        parsed = parsed_obj.model_dump()
    except (ValidationError, ValueError, TypeError) as exc:
        logger.warning("Structured output parse failed: %s", exc)
        parsed = {
            "message": "I could not parse structured output from the model.",
            "trades": [],
            "watchlist_changes": [],
        }

    return _normalize_response(parsed)
