"""Portfolio endpoints: current holdings, trade execution, value history."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import repo
from app.runtime import get_price_cache
from app.services.portfolio import TradeError, build_portfolio, execute_trade

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


class TradeBody(BaseModel):
    ticker: str = Field(..., min_length=1)
    quantity: float = Field(..., gt=0)
    side: str


@router.get("")
async def get_portfolio() -> dict:
    return build_portfolio()


@router.get("/history")
async def get_history() -> dict:
    return {"snapshots": repo.get_snapshots()}


@router.post("/trade")
async def post_trade(body: TradeBody) -> dict:
    ticker = body.ticker.strip().upper()
    price = get_price_cache().get_price(ticker)
    if price is None:
        raise HTTPException(status_code=400, detail=f"No price available for {ticker}.")
    try:
        trade = execute_trade(ticker, body.side, body.quantity, price)
    except TradeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "trade": trade, "portfolio": build_portfolio()}
