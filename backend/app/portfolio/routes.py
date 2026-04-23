"""HTTP routes for the portfolio domain."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .. import db
from ..state import AppState, get_state
from . import service


class TradeRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)
    quantity: float = Field(..., gt=0)
    side: Literal["buy", "sell"]


class PositionResponse(BaseModel):
    ticker: str
    quantity: float
    avg_cost: float
    current_price: float | None
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_percent: float


class PortfolioResponse(BaseModel):
    cash_balance: float
    positions: list[PositionResponse]
    market_value: float
    total_value: float
    unrealized_pnl: float
    unrealized_pnl_percent: float


class TradeResponse(BaseModel):
    ticker: str
    side: Literal["buy", "sell"]
    quantity: float
    price: float
    cash_balance_after: float
    position_quantity_after: float
    executed_at: str


class HistoryPoint(BaseModel):
    total_value: float
    recorded_at: str


router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


@router.get("", response_model=PortfolioResponse)
def get_portfolio(state: AppState = Depends(get_state)) -> PortfolioResponse:
    view = service.get_portfolio_view(state.price_cache)
    return PortfolioResponse(
        cash_balance=view.cash_balance,
        positions=[PositionResponse(**p.__dict__) for p in view.positions],
        market_value=view.market_value,
        total_value=view.total_value,
        unrealized_pnl=view.unrealized_pnl,
        unrealized_pnl_percent=view.unrealized_pnl_percent,
    )


@router.post("/trade", response_model=TradeResponse)
def post_trade(
    body: TradeRequest, state: AppState = Depends(get_state)
) -> TradeResponse:
    try:
        result = service.execute_trade(
            state.price_cache, body.ticker, body.side, body.quantity
        )
    except service.TradeError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return TradeResponse(**result.__dict__)


@router.get("/history", response_model=list[HistoryPoint])
def get_history() -> list[HistoryPoint]:
    snaps = db.snapshots.list_history()
    return [HistoryPoint(total_value=s.total_value, recorded_at=s.recorded_at) for s in snaps]
