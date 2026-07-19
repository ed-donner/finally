"""Structured-output schema for the chat assistant (PLAN §9)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LLMTrade(BaseModel):
    """A trade the assistant wants to execute."""

    ticker: str
    side: Literal["buy", "sell"]
    quantity: float = Field(..., gt=0)


class WatchlistChange(BaseModel):
    """A watchlist modification the assistant wants to make."""

    ticker: str
    action: Literal["add", "remove"]


class ChatResponse(BaseModel):
    """The complete structured response returned by the LLM."""

    message: str
    trades: list[LLMTrade] = Field(default_factory=list)
    watchlist_changes: list[WatchlistChange] = Field(default_factory=list)
