"""FastAPI router factory for portfolio endpoints."""

from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, HTTPException

from app.market.cache import PriceCache
from app.portfolio import service
from app.portfolio.models import (
    PortfolioHistoryResponse,
    PortfolioResponse,
    TradeRequest,
    TradeResponse,
)
from app.portfolio.snapshots import record_snapshot


def create_portfolio_router(
    db: aiosqlite.Connection, price_cache: PriceCache
) -> APIRouter:
    """Create the portfolio router with injected dependencies.

    Follows the same closure-based factory pattern as create_stream_router.
    """
    router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

    @router.get("", response_model=PortfolioResponse)
    async def get_portfolio():
        """Return current portfolio with positions, cash, and total value."""
        return await service.get_portfolio(db, price_cache)

    @router.post("/trade", response_model=TradeResponse)
    async def post_trade(request: TradeRequest):
        """Execute a market order (buy or sell)."""
        try:
            result = await service.execute_trade(
                db, price_cache, request.ticker, request.side, request.quantity
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await record_snapshot(db, price_cache)
        return result

    @router.get("/history", response_model=PortfolioHistoryResponse)
    async def get_portfolio_history():
        """Return portfolio value snapshots over time."""
        return await service.get_portfolio_history(db)

    return router
