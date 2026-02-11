"""FastAPI router factory for watchlist endpoints."""

from __future__ import annotations

import aiosqlite
from fastapi import APIRouter

from app.market.cache import PriceCache
from app.market.interface import MarketDataSource

from . import service
from .models import AddTickerRequest, WatchlistItem, WatchlistResponse


def create_watchlist_router(
    db: aiosqlite.Connection,
    price_cache: PriceCache,
    market_data_source: MarketDataSource,
) -> APIRouter:
    """Create the watchlist router with injected dependencies.

    Follows the same closure-based factory pattern as create_stream_router.
    """
    router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

    @router.get("")
    async def get_watchlist() -> WatchlistResponse:
        """Return all watchlist tickers enriched with live price data."""
        rows = await service.get_watchlist(db)
        items = []
        for row in rows:
            ticker = row["ticker"]
            price_update = price_cache.get(ticker)
            item = WatchlistItem(
                ticker=ticker,
                added_at=row["added_at"],
                price=price_update.price if price_update else None,
                change=price_update.change if price_update else None,
                change_percent=price_update.change_percent if price_update else None,
                direction=price_update.direction if price_update else None,
            )
            items.append(item)
        return WatchlistResponse(items=items, count=len(items))

    @router.post("", status_code=201)
    async def add_ticker_endpoint(request: AddTickerRequest) -> WatchlistItem:
        """Add a ticker to the watchlist and start tracking its price."""
        result = await service.add_ticker(db, request.ticker)
        ticker = result["ticker"]
        await market_data_source.add_ticker(ticker)
        return WatchlistItem(ticker=ticker, added_at=result["added_at"])

    @router.delete("/{ticker}", status_code=200)
    async def remove_ticker_endpoint(ticker: str) -> dict:
        """Remove a ticker from the watchlist and stop tracking its price."""
        normalized = ticker.upper().strip()
        await service.remove_ticker(db, ticker)
        await market_data_source.remove_ticker(normalized)
        return {"removed": normalized}

    return router
