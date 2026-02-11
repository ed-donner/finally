"""Pydantic schemas for LLM chat request, response, and action types."""

from pydantic import BaseModel, Field


class TradeAction(BaseModel):
    """A trade the LLM wants to execute."""

    ticker: str
    side: str = Field(pattern=r"^(buy|sell)$")
    quantity: float = Field(gt=0)


class WatchlistAction(BaseModel):
    """A watchlist change the LLM wants to make."""

    ticker: str
    action: str = Field(pattern=r"^(add|remove)$")


class ChatLLMResponse(BaseModel):
    """Schema for the LLM's structured JSON response."""

    message: str
    trades: list[TradeAction] = []
    watchlist_changes: list[WatchlistAction] = []


class ChatRequest(BaseModel):
    """Incoming chat request from the frontend."""

    message: str = Field(min_length=1)


class TradeResult(BaseModel):
    """Result of executing a single trade action."""

    status: str
    ticker: str
    side: str
    quantity: float | None = None
    price: float | None = None
    total: float | None = None
    error: str | None = None


class WatchlistResult(BaseModel):
    """Result of executing a single watchlist change."""

    status: str
    ticker: str
    action: str
    error: str | None = None


class ChatResponse(BaseModel):
    """Complete chat response returned to the frontend."""

    message: str
    trades: list[TradeResult] = []
    watchlist_changes: list[WatchlistResult] = []
