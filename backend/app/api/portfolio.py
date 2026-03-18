"""Portfolio API endpoints."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.db import (
    get_positions,
    get_position_by_ticker,
    get_user_profile,
    insert_portfolio_snapshot,
    insert_trade,
    list_portfolio_snapshots,
    update_cash_balance,
    upsert_position,
)

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


class TradeRequest(BaseModel):
    ticker: str = Field(..., pattern=r"^[A-Z]{1,5}$")
    quantity: float = Field(..., gt=0)
    side: str = Field(..., pattern=r"^(buy|sell)$")


async def _build_portfolio_summary(cache) -> dict:
    """Build a full portfolio summary with current prices."""
    profile = await get_user_profile()
    positions = await get_positions()
    cash = profile["cash_balance"]
    total_value = cash
    enriched = []
    for pos in positions:
        ticker = pos["ticker"]
        price_update = cache.get(ticker)
        current_price = price_update.price if price_update else pos["avg_cost"]
        market_value = current_price * pos["quantity"]
        cost_basis = pos["avg_cost"] * pos["quantity"]
        unrealized_pnl = market_value - cost_basis
        pnl_percent = (unrealized_pnl / cost_basis * 100) if cost_basis != 0 else 0.0
        total_value += market_value
        enriched.append({
            "ticker": ticker,
            "quantity": pos["quantity"],
            "avg_cost": round(pos["avg_cost"], 2),
            "current_price": round(current_price, 2),
            "market_value": round(market_value, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "pnl_pct": round(pnl_percent, 2),
        })
    return {
        "cash_balance": round(cash, 2),
        "total_value": round(total_value, 2),
        "positions": enriched,
    }


@router.get("")
async def get_portfolio(request: Request):
    """Current positions, cash balance, total value, unrealized P&L."""
    cache = request.app.state.price_cache
    return await _build_portfolio_summary(cache)


@router.post("/trade")
async def execute_trade(trade: TradeRequest, request: Request):
    """Execute a market order trade."""
    cache = request.app.state.price_cache
    ticker = trade.ticker.upper()

    # Get current price
    price_update = cache.get(ticker)
    if price_update is None:
        raise HTTPException(status_code=400, detail=f"No price available for {ticker}")
    current_price = price_update.price

    profile = await get_user_profile()
    cash = profile["cash_balance"]

    if trade.side == "buy":
        cost = current_price * trade.quantity
        if cost > cash:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient cash. Need ${cost:.2f}, have ${cash:.2f}",
            )
        new_cash = cash - cost

        # Update position (calculate new avg cost)
        existing = await get_position_by_ticker(ticker)
        if existing:
            total_qty = existing["quantity"] + trade.quantity
            new_avg = (existing["avg_cost"] * existing["quantity"] + cost) / total_qty
        else:
            total_qty = trade.quantity
            new_avg = current_price

        await update_cash_balance(new_cash)
        await upsert_position(ticker, total_qty, new_avg)

    else:  # sell
        existing = await get_position_by_ticker(ticker)
        if not existing or existing["quantity"] < trade.quantity:
            owned = existing["quantity"] if existing else 0
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient shares. Want to sell {trade.quantity}, own {owned}",
            )

        proceeds = current_price * trade.quantity
        new_cash = cash + proceeds
        remaining_qty = existing["quantity"] - trade.quantity

        await update_cash_balance(new_cash)
        await upsert_position(ticker, remaining_qty, existing["avg_cost"])

    # Record the trade
    trade_record = await insert_trade(ticker, trade.side, trade.quantity, current_price)

    # Snapshot portfolio value after trade
    portfolio = await _build_portfolio_summary(cache)
    await insert_portfolio_snapshot(portfolio["total_value"])

    return {"trade": trade_record, "portfolio": portfolio}


@router.get("/history")
async def get_portfolio_history():
    """Portfolio value snapshots over time."""
    snapshots = await list_portfolio_snapshots()
    # Return in chronological order as a plain array
    return list(reversed(snapshots))
