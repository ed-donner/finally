"""Portfolio API routes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.market import PriceCache

from .service import PortfolioService


class TradeRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)
    side: str = Field(..., pattern="^(buy|sell)$")
    quantity: float = Field(..., gt=0)


def create_portfolio_router(price_cache: PriceCache) -> APIRouter:
    """Create portfolio API router with injected dependencies."""
    router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

    def _service() -> PortfolioService:
        from app.db import get_db
        return PortfolioService(get_db(), price_cache)

    @router.get("")
    def get_portfolio():
        return _service().get_portfolio()

    @router.post("/trade")
    def execute_trade(req: TradeRequest):
        try:
            result = _service().execute_trade(req.ticker.upper(), req.side, req.quantity)
            return {
                "ticker": result.ticker,
                "side": result.side,
                "quantity": result.quantity,
                "price": result.price,
                "total_cost": result.total_cost,
                "cash_after": result.cash_after,
            }
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.get("/history")
    def get_history():
        return _service().get_history()

    return router
