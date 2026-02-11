"""Pydantic v2 models for watchlist API requests and responses."""

from pydantic import BaseModel, Field


class AddTickerRequest(BaseModel):
    """Request body for adding a ticker to the watchlist."""

    ticker: str = Field(..., min_length=1, max_length=10)


class WatchlistItem(BaseModel):
    """A single watchlist entry, optionally enriched with live price data."""

    ticker: str
    added_at: str
    price: float | None = None
    change: float | None = None
    change_percent: float | None = None
    direction: str | None = None


class WatchlistResponse(BaseModel):
    """Response containing the full watchlist."""

    items: list[WatchlistItem]
    count: int
