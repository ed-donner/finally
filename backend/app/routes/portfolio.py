"""Portfolio API endpoints."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.db import (
    delete_position,
    get_cash_balance,
    get_portfolio_history,
    get_position,
    get_positions,
    insert_snapshot,
    insert_trade,
    update_cash_balance,
    upsert_position,
)

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


class TradeRequest(BaseModel):
    ticker: str
    side: str  # "buy" or "sell"
    quantity: float


@router.get("")
async def get_portfolio(request: Request):
    """Current positions, cash balance, total value, unrealized P&L."""
    cache = request.app.state.price_cache
    positions = await get_positions()
    cash = await get_cash_balance()

    enriched = []
    total_market_value = 0.0
    total_unrealized_pnl = 0.0

    for pos in positions:
        current_price = cache.get_price(pos["ticker"]) or pos["avg_cost"]
        market_value = current_price * pos["quantity"]
        cost_basis = pos["avg_cost"] * pos["quantity"]
        unrealized_pnl = market_value - cost_basis
        pnl_percent = (unrealized_pnl / cost_basis * 100) if cost_basis else 0.0

        enriched.append({
            "ticker": pos["ticker"],
            "quantity": pos["quantity"],
            "avg_cost": pos["avg_cost"],
            "current_price": current_price,
            "market_value": round(market_value, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "pnl_percent": round(pnl_percent, 2),
        })

        total_market_value += market_value
        total_unrealized_pnl += unrealized_pnl

    total_value = cash + total_market_value

    return {
        "positions": enriched,
        "cash": round(cash, 2),
        "total_market_value": round(total_market_value, 2),
        "total_value": round(total_value, 2),
        "unrealized_pnl": round(total_unrealized_pnl, 2),
    }


@router.post("/trade")
async def execute_trade(trade: TradeRequest, request: Request):
    """Execute a market order. Validates cash/shares, updates DB, records snapshot."""
    cache = request.app.state.price_cache
    ticker = trade.ticker.upper()
    side = trade.side.lower()
    quantity = trade.quantity

    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be positive")

    current_price = cache.get_price(ticker)
    if current_price is None:
        raise HTTPException(status_code=400, detail=f"No price available for {ticker}")

    cash = await get_cash_balance()

    if side == "buy":
        cost = current_price * quantity
        if cost > cash:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient cash. Need ${cost:.2f}, have ${cash:.2f}",
            )

        # Update cash
        await update_cash_balance(cash - cost)

        # Update position
        existing = await get_position(ticker)
        if existing:
            total_qty = existing["quantity"] + quantity
            total_cost = (existing["avg_cost"] * existing["quantity"]) + cost
            new_avg = total_cost / total_qty
            await upsert_position(ticker, total_qty, new_avg)
        else:
            await upsert_position(ticker, quantity, current_price)

    else:  # sell
        existing = await get_position(ticker)
        if not existing or existing["quantity"] < quantity:
            held = existing["quantity"] if existing else 0
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient shares. Have {held}, trying to sell {quantity}",
            )

        proceeds = current_price * quantity
        await update_cash_balance(cash + proceeds)

        remaining = existing["quantity"] - quantity
        if remaining > 0:
            await upsert_position(ticker, remaining, existing["avg_cost"])
        else:
            await delete_position(ticker)

    # Record trade
    trade_record = await insert_trade(ticker, side, quantity, current_price)

    # Record portfolio snapshot
    new_cash = await get_cash_balance()
    positions = await get_positions()
    total_value = new_cash
    for pos in positions:
        price = cache.get_price(pos["ticker"]) or pos["avg_cost"]
        total_value += price * pos["quantity"]
    await insert_snapshot(round(total_value, 2))

    return {
        "trade": trade_record,
        "cash": round(new_cash, 2),
        "total_value": round(total_value, 2),
    }


@router.get("/history")
async def portfolio_history():
    """Portfolio value snapshots over time."""
    snapshots = await get_portfolio_history()
    return {"snapshots": snapshots}
