"""Portfolio endpoints: valuation, trading and value history."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services import portfolio as portfolio_service
from app.services.watchlist import sync_tracked_tickers

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


class TradeRequest(BaseModel):
    # Loose types on purpose: validation lives in the service so REST and LLM share
    # the same rules and every failure is a 400 with a user-facing message.
    ticker: str
    quantity: float
    side: str


@router.get("")
async def get_portfolio(request: Request) -> dict:
    return portfolio_service.get_portfolio(request.app.state.price_cache)


@router.post("/trade")
async def trade(body: TradeRequest, request: Request) -> dict:
    cache = request.app.state.price_cache
    try:
        executed = portfolio_service.execute_trade(body.ticker, body.side, body.quantity, cache)
    except portfolio_service.TradeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await sync_tracked_tickers(request.app.state.market_source)
    return {"trade": executed, "portfolio": portfolio_service.get_portfolio(cache)}


@router.get("/history")
async def history() -> dict:
    return {"snapshots": portfolio_service.get_history()}
