"""Pydantic models for LLM structured output and the action-result envelope."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Side = Literal["buy", "sell"]
TradeIntent = Literal["recommend", "execute"]
TradeActionStatus = Literal["recommended", "approval_required", "executed", "failed"]
WatchlistAction = Literal["add", "remove"]


class TradeRequest(BaseModel):
    """A single trade the LLM wants to recommend or execute."""

    ticker: str = Field(..., description="Stock ticker, uppercase")
    side: Side
    quantity: float = Field(..., gt=0, description="Number of shares (fractional allowed)")
    intent: TradeIntent = Field(
        default="recommend",
        description=(
            "'recommend' for analysis-only ideas, 'execute' only when the user clearly asked "
            "to place the order"
        ),
    )


class WatchlistChange(BaseModel):
    """A single watchlist add/remove the LLM wants to make."""
    ticker: str = Field(..., description="Stock ticker, uppercase")
    action: WatchlistAction


class LLMResponse(BaseModel):
    """The structured output schema the LLM must return.

    Both arrays are required (may be empty) — see PLAN.md §9.
    """
    message: str = Field(..., description="Conversational reply shown to the user")
    trades: list[TradeRequest] = Field(default_factory=list)
    watchlist_changes: list[WatchlistChange] = Field(default_factory=list)


class TradeActionResult(BaseModel):
    """Per-trade outcome attached to the response payload + persisted in chat_messages."""

    ticker: str
    side: Side
    quantity: float
    intent: TradeIntent
    status: TradeActionStatus
    price: float | None = None
    cash_balance_after: float | None = None
    error: str | None = None


class WatchlistActionResult(BaseModel):
    ticker: str
    action: WatchlistAction
    ok: bool
    error: str | None = None


class ChatActions(BaseModel):
    """Aggregate action results for a single assistant turn."""
    trades: list[TradeActionResult] = Field(default_factory=list)
    watchlist_changes: list[WatchlistActionResult] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.trades and not self.watchlist_changes
