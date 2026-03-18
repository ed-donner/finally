"""Pydantic models for LLM structured output."""

from typing import Literal

from pydantic import BaseModel


class TradeAction(BaseModel):
    ticker: str
    side: Literal["buy", "sell"]
    quantity: float


class WatchlistChange(BaseModel):
    ticker: str
    action: Literal["add", "remove"]


class LLMResponse(BaseModel):
    message: str
    trades: list[TradeAction] = []
    watchlist_changes: list[WatchlistChange] = []
