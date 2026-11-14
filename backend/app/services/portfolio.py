"""Portfolio service: trade execution, valuation and snapshots.

Shared by the REST routes and the LLM chat flow so trade validation is identical.
"""

from __future__ import annotations

import math
import re

from app import db
from app.market import PriceCache, normalize_ticker

EPSILON = 1e-9
VALID_SIDES = ("buy", "sell")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class TradeError(Exception):
    """A trade was rejected. ``str(exc)`` is a user-facing message."""


def _money(value: float) -> float:
    return round(value, 2)


def _fmt_qty(quantity: float) -> str:
    return f"{quantity:g}"


def validate_ticker(ticker: str) -> str:
    """Normalize and validate a ticker symbol. Raises ValueError with a user-facing message."""
    if not isinstance(ticker, str):
        raise ValueError("Ticker must be a string")
    norm = normalize_ticker(ticker)
    if not TICKER_RE.match(norm):
        raise ValueError(f"Invalid ticker: {ticker!r}")
    return norm


def execute_trade(ticker: str, side: str, quantity: float, price_cache: PriceCache) -> dict:
    """Execute a market order at the current cached price.

    Atomically updates cash, the position and the trade log, then records a portfolio
    snapshot. Returns the trade dict ``{id, ticker, side, quantity, price, executed_at}``.
    Raises TradeError (user-facing message) on any validation failure.
    """
    try:
        ticker = validate_ticker(ticker)
    except ValueError as exc:
        raise TradeError(str(exc)) from None

    side = (side or "").strip().lower() if isinstance(side, str) else ""
    if side not in VALID_SIDES:
        raise TradeError("Side must be 'buy' or 'sell'")

    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        raise TradeError("Quantity must be a number") from None
    if not math.isfinite(quantity) or quantity <= 0:
        raise TradeError("Quantity must be greater than 0")

    price = price_cache.get_price(ticker)
    if price is None or price <= 0:
        raise TradeError(f"No price available for {ticker} yet; add it to the watchlist first")

    cost = quantity * price
    with db.transaction() as conn:
        cash = db.get_cash_balance(conn=conn)
        position = db.get_position(ticker, conn=conn)
        held = float(position["quantity"]) if position else 0.0
        avg_cost = float(position["avg_cost"]) if position else 0.0

        if side == "buy":
            if cost > cash + EPSILON:
                raise TradeError(
                    f"Insufficient cash: buying {_fmt_qty(quantity)} {ticker} at "
                    f"${price:,.2f} costs ${cost:,.2f} but only ${cash:,.2f} is available"
                )
            new_qty = held + quantity
            new_avg = (held * avg_cost + quantity * price) / new_qty
            db.set_cash_balance(max(cash - cost, 0.0), conn=conn)
            db.upsert_position(ticker, new_qty, new_avg, conn=conn)
        else:
            if quantity > held + EPSILON:
                raise TradeError(
                    f"Insufficient shares: cannot sell {_fmt_qty(quantity)} {ticker}, "
                    f"you hold {_fmt_qty(held)}"
                )
            remaining = held - quantity
            db.set_cash_balance(cash + cost, conn=conn)
            if remaining <= EPSILON:
                db.delete_position(ticker, conn=conn)
            else:
                db.upsert_position(ticker, remaining, avg_cost, conn=conn)

        trade = db.record_trade(ticker, side, quantity, price, conn=conn)

    record_portfolio_snapshot(price_cache)
    return {
        "id": trade["id"],
        "ticker": trade["ticker"],
        "side": trade["side"],
        "quantity": float(trade["quantity"]),
        "price": float(trade["price"]),
        "executed_at": trade["executed_at"],
    }


def _valuation(price_cache: PriceCache) -> tuple[float, list[dict]]:
    cash = db.get_cash_balance()
    positions = []
    for p in db.get_positions():
        qty = float(p["quantity"])
        avg = float(p["avg_cost"])
        current = price_cache.get_price(p["ticker"])
        if current is None:
            current = avg
        positions.append(
            {"ticker": p["ticker"], "quantity": qty, "avg_cost": avg, "current_price": current}
        )
    return cash, positions


def get_portfolio(price_cache: PriceCache) -> dict:
    """Current portfolio valuation; the GET /api/portfolio response shape."""
    cash, raw = _valuation(price_cache)
    positions_value = sum(p["quantity"] * p["current_price"] for p in raw)
    cost_basis = sum(p["quantity"] * p["avg_cost"] for p in raw)
    total_value = cash + positions_value
    unrealized = positions_value - cost_basis

    positions = []
    for p in raw:
        market_value = p["quantity"] * p["current_price"]
        basis = p["quantity"] * p["avg_cost"]
        pnl = market_value - basis
        positions.append(
            {
                "ticker": p["ticker"],
                "quantity": p["quantity"],
                "avg_cost": _money(p["avg_cost"]),
                "current_price": _money(p["current_price"]),
                "market_value": _money(market_value),
                "unrealized_pnl": _money(pnl),
                "unrealized_pnl_percent": _money(pnl / basis * 100) if basis else 0.0,
                "weight": round(market_value / total_value, 4) if total_value else 0.0,
            }
        )

    return {
        "cash_balance": _money(cash),
        "positions_value": _money(positions_value),
        "total_value": _money(total_value),
        "unrealized_pnl": _money(unrealized),
        "unrealized_pnl_percent": _money(unrealized / cost_basis * 100) if cost_basis else 0.0,
        "positions": positions,
    }


def get_total_value(price_cache: PriceCache) -> float:
    cash, raw = _valuation(price_cache)
    return cash + sum(p["quantity"] * p["current_price"] for p in raw)


def record_portfolio_snapshot(price_cache: PriceCache) -> dict:
    """Record the current total portfolio value in portfolio_snapshots."""
    return db.record_snapshot(_money(get_total_value(price_cache)))


def get_history(limit: int = 1000) -> list[dict]:
    """Portfolio value snapshots, oldest first: ``[{total_value, recorded_at}]``."""
    return [
        {"total_value": float(s["total_value"]), "recorded_at": s["recorded_at"]}
        for s in db.get_snapshots(limit=limit)
    ]
