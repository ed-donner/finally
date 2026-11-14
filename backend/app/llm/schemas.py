"""Structured-output schemas for the FinAlly chat assistant."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

FALLBACK_MESSAGE = (
    "Sorry, I couldn't make sense of my own response just now. Please try asking again."
)


class TradeInstruction(BaseModel):
    """A market order the assistant wants executed."""

    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(description="Ticker symbol, e.g. AAPL")
    side: Literal["buy", "sell"]
    quantity: float = Field(description="Number of shares (fractional allowed), > 0")


class WatchlistChange(BaseModel):
    """A watchlist modification the assistant wants applied."""

    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(description="Ticker symbol, e.g. PYPL")
    action: Literal["add", "remove"]


class ChatLLMResponse(BaseModel):
    """The JSON object the LLM must return (sent as `response_format`).

    All fields are required (no defaults) so the generated JSON schema is valid for
    strict structured outputs; use empty lists when there is nothing to do.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(description="Conversational reply shown to the user")
    trades: list[TradeInstruction] = Field(description="Trades to execute now; [] if none")
    watchlist_changes: list[WatchlistChange] = Field(
        description="Watchlist changes to apply now; [] if none"
    )


_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def _extract_json_object(raw: str) -> Any:
    """Best-effort JSON extraction: plain JSON, ```json fenced``` JSON, or first {...} block."""
    text = raw.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("no JSON object found")


def _parse_items(items: Any, model: type[BaseModel], key: str) -> list:
    """Validate list items one by one, dropping malformed entries instead of failing."""
    if not isinstance(items, list):
        return []
    parsed = []
    for item in items:
        if not isinstance(item, dict):
            continue
        cleaned = dict(item)
        if isinstance(cleaned.get("ticker"), str):
            cleaned["ticker"] = cleaned["ticker"].strip().upper()
        if isinstance(cleaned.get(key), str):
            cleaned[key] = cleaned[key].strip().lower()
        try:
            parsed.append(model.model_validate(cleaned, strict=False))
        except ValidationError:
            logger.warning("Dropping malformed %s item from LLM response: %r", model.__name__, item)
    return parsed


def parse_llm_response(raw: str | None) -> ChatLLMResponse:
    """Parse the raw LLM content into a ChatLLMResponse. Never raises.

    - Missing `trades` / `watchlist_changes` are treated as empty.
    - Malformed individual actions are dropped; valid ones are kept.
    - Unparseable content: if it looks like plain prose, it becomes the message;
      otherwise a generic fallback message is returned.
    """
    if not raw or not raw.strip():
        return ChatLLMResponse(message=FALLBACK_MESSAGE, trades=[], watchlist_changes=[])
    try:
        data = _extract_json_object(raw)
    except (ValueError, json.JSONDecodeError):
        logger.warning("LLM returned non-JSON content")
        text = raw.strip()
        looks_like_json = text.startswith("{") or text.startswith("[") or text.startswith("```")
        message = FALLBACK_MESSAGE if looks_like_json else text
        return ChatLLMResponse(message=message, trades=[], watchlist_changes=[])

    if not isinstance(data, dict):
        return ChatLLMResponse(message=FALLBACK_MESSAGE, trades=[], watchlist_changes=[])

    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        message = FALLBACK_MESSAGE
    return ChatLLMResponse(
        message=message.strip(),
        trades=_parse_items(data.get("trades"), TradeInstruction, "side"),
        watchlist_changes=_parse_items(data.get("watchlist_changes"), WatchlistChange, "action"),
    )
