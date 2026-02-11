"""Watchlist API routes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.market import PriceCache

from .service import WatchlistService


class AddTickerRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)


def create_watchlist_router(price_cache: PriceCache) -> APIRouter:
    """Create watchlist API router with injected dependencies."""
    router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

    def _service() -> WatchlistService:
        from app.db import get_db
        return WatchlistService(get_db(), price_cache)

    @router.get("")
    def get_watchlist():
        return _service().get_watchlist()

    @router.post("")
    def add_ticker(req: AddTickerRequest):
        try:
            return _service().add_ticker(req.ticker)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

    @router.delete("/{ticker}")
    def remove_ticker(ticker: str):
        try:
            _service().remove_ticker(ticker)
            return {"status": "removed", "ticker": ticker.upper()}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    return router
