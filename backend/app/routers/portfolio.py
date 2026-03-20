"""Portfolio API endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db.connection import get_db
from ..market.cache import PriceCache

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

_price_cache: PriceCache | None = None


def set_price_cache(cache: PriceCache) -> None:
    global _price_cache
    _price_cache = cache


class TradeRequest(BaseModel):
    ticker: str
    quantity: float
    side: str  # "buy" or "sell"


def _get_portfolio_data(db: Any) -> dict[str, Any]:
    """Build portfolio response dict from DB and price cache."""
    profile = db.execute(
        "SELECT cash_balance FROM users_profile WHERE id = 'default'"
    ).fetchone()
    cash = profile["cash_balance"] if profile else 10000.0

    positions_rows = db.execute(
        "SELECT ticker, quantity, avg_cost, updated_at FROM positions WHERE user_id = 'default' AND quantity > 0"
    ).fetchall()

    positions = []
    total_position_value = 0.0

    for row in positions_rows:
        ticker = row["ticker"]
        quantity = row["quantity"]
        avg_cost = row["avg_cost"]
        current_price = _price_cache.get_price(ticker) if _price_cache else None

        if current_price is None:
            current_price = avg_cost  # fallback

        market_value = quantity * current_price
        cost_basis = quantity * avg_cost
        unrealized_pnl = market_value - cost_basis
        pnl_percent = (unrealized_pnl / cost_basis * 100) if cost_basis else 0.0

        total_position_value += market_value
        positions.append({
            "ticker": ticker,
            "quantity": quantity,
            "avg_cost": round(avg_cost, 4),
            "current_price": round(current_price, 2),
            "market_value": round(market_value, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "pnl_percent": round(pnl_percent, 2),
            "updated_at": row["updated_at"],
        })

    total_value = cash + total_position_value

    return {
        "cash_balance": round(cash, 2),
        "total_value": round(total_value, 2),
        "positions_value": round(total_position_value, 2),
        "positions": positions,
    }


@router.get("")
def get_portfolio() -> dict[str, Any]:
    """Return current portfolio: positions, cash, total value."""
    with get_db() as db:
        return _get_portfolio_data(db)


@router.get("/history")
def get_portfolio_history() -> list[dict[str, Any]]:
    """Return portfolio value snapshots over time."""
    with get_db() as db:
        rows = db.execute(
            "SELECT total_value, recorded_at FROM portfolio_snapshots WHERE user_id = 'default' ORDER BY recorded_at DESC LIMIT 500"
        ).fetchall()

    return [{"total_value": r["total_value"], "recorded_at": r["recorded_at"]} for r in reversed(rows)]


@router.post("/trade")
def execute_trade(request: TradeRequest) -> dict[str, Any]:
    """Execute a market order trade."""
    ticker = request.ticker.upper().strip()
    quantity = request.quantity
    side = request.side.lower()

    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="Side must be 'buy' or 'sell'")
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be positive")

    # Get current price
    current_price = _price_cache.get_price(ticker) if _price_cache else None
    if current_price is None:
        raise HTTPException(status_code=400, detail=f"No price available for {ticker}")

    now = datetime.now(timezone.utc).isoformat()
    trade_id = str(uuid.uuid4())

    with get_db() as db:
        profile = db.execute(
            "SELECT cash_balance FROM users_profile WHERE id = 'default'"
        ).fetchone()
        cash = profile["cash_balance"] if profile else 10000.0

        if side == "buy":
            cost = quantity * current_price
            if cost > cash:
                raise HTTPException(
                    status_code=400,
                    detail=f"Insufficient cash. Need ${cost:.2f}, have ${cash:.2f}"
                )

            # Update cash
            db.execute(
                "UPDATE users_profile SET cash_balance = cash_balance - ? WHERE id = 'default'",
                (cost,),
            )

            # Upsert position
            existing = db.execute(
                "SELECT quantity, avg_cost FROM positions WHERE user_id = 'default' AND ticker = ?",
                (ticker,),
            ).fetchone()

            if existing:
                old_qty = existing["quantity"]
                old_avg = existing["avg_cost"]
                new_qty = old_qty + quantity
                new_avg = (old_qty * old_avg + quantity * current_price) / new_qty
                db.execute(
                    "UPDATE positions SET quantity = ?, avg_cost = ?, updated_at = ? WHERE user_id = 'default' AND ticker = ?",
                    (new_qty, new_avg, now, ticker),
                )
            else:
                db.execute(
                    "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) VALUES (?, 'default', ?, ?, ?, ?)",
                    (str(uuid.uuid4()), ticker, quantity, current_price, now),
                )

        else:  # sell
            existing = db.execute(
                "SELECT quantity, avg_cost FROM positions WHERE user_id = 'default' AND ticker = ?",
                (ticker,),
            ).fetchone()

            if not existing or existing["quantity"] < quantity:
                owned = existing["quantity"] if existing else 0
                raise HTTPException(
                    status_code=400,
                    detail=f"Insufficient shares. Want to sell {quantity}, own {owned}"
                )

            proceeds = quantity * current_price
            new_qty = existing["quantity"] - quantity

            db.execute(
                "UPDATE users_profile SET cash_balance = cash_balance + ? WHERE id = 'default'",
                (proceeds,),
            )

            if new_qty > 0.0001:
                db.execute(
                    "UPDATE positions SET quantity = ?, updated_at = ? WHERE user_id = 'default' AND ticker = ?",
                    (new_qty, now, ticker),
                )
            else:
                db.execute(
                    "DELETE FROM positions WHERE user_id = 'default' AND ticker = ?",
                    (ticker,),
                )

        # Log trade
        db.execute(
            "INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at) VALUES (?, 'default', ?, ?, ?, ?, ?)",
            (trade_id, ticker, side, quantity, current_price, now),
        )

        # Snapshot portfolio
        portfolio = _get_portfolio_data(db)
        db.execute(
            "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) VALUES (?, 'default', ?, ?)",
            (str(uuid.uuid4()), portfolio["total_value"], now),
        )

    return {
        "trade_id": trade_id,
        "ticker": ticker,
        "side": side,
        "quantity": quantity,
        "price": current_price,
        "executed_at": now,
        "portfolio": portfolio,
    }
