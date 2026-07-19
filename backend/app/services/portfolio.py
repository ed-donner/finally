"""Portfolio valuation and trade execution.

Owns the buy/sell math, validation, position bookkeeping, and snapshotting.
Prices come from the shared :class:`PriceCache` (via :mod:`app.runtime`).
See TEAM_CONTRACT.md §2 and §3 for the exact interface and wire shapes.
"""

from __future__ import annotations

from app.db import repo
from app.runtime import get_price_cache

# Positions below this quantity are treated as fully closed (float dust).
_EPSILON = 1e-9


class TradeError(Exception):
    """Raised when a trade fails validation (insufficient cash/shares, bad input)."""


def _price_for(ticker: str) -> float | None:
    return get_price_cache().get_price(ticker)


def build_portfolio() -> dict:
    """Return the full portfolio snapshot (the GET /api/portfolio shape)."""
    profile = repo.get_profile()
    cash_balance = float(profile.get("cash_balance", 0.0))

    positions_out: list[dict] = []
    positions_value = 0.0
    total_unrealized_pl = 0.0

    for pos in repo.get_positions():
        ticker = pos["ticker"]
        quantity = float(pos["quantity"])
        avg_cost = float(pos["avg_cost"])
        # Fall back to avg_cost if no live tick yet, so valuation is never null.
        current_price = _price_for(ticker)
        if current_price is None:
            current_price = avg_cost

        market_value = quantity * current_price
        unrealized_pl = (current_price - avg_cost) * quantity
        unrealized_pl_pct = (
            (current_price - avg_cost) / avg_cost * 100 if avg_cost else 0.0
        )

        positions_value += market_value
        total_unrealized_pl += unrealized_pl

        positions_out.append(
            {
                "ticker": ticker,
                "quantity": round(quantity, 6),
                "avg_cost": round(avg_cost, 4),
                "current_price": round(current_price, 4),
                "market_value": round(market_value, 2),
                "unrealized_pl": round(unrealized_pl, 2),
                "unrealized_pl_pct": round(unrealized_pl_pct, 2),
            }
        )

    total_value = cash_balance + positions_value
    return {
        "cash_balance": round(cash_balance, 2),
        "total_value": round(total_value, 2),
        "positions_value": round(positions_value, 2),
        "total_unrealized_pl": round(total_unrealized_pl, 2),
        "positions": positions_out,
    }


def execute_trade(ticker: str, side: str, quantity: float, price: float) -> dict:
    """Execute a market order. Returns the inserted trade row.

    Raises :class:`TradeError` on validation failure. On success: updates cash
    and the position, appends the trade, and records a portfolio snapshot.
    """
    ticker = ticker.strip().upper()
    side = side.strip().lower()

    if side not in {"buy", "sell"}:
        raise TradeError(f"Invalid side '{side}' — must be 'buy' or 'sell'.")
    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        raise TradeError("Quantity must be a number.") from None
    if quantity <= 0:
        raise TradeError("Quantity must be greater than zero.")
    if price is None or price <= 0:
        raise TradeError(f"No valid price available for {ticker}.")

    cash = float(repo.get_profile().get("cash_balance", 0.0))
    existing = repo.get_position(ticker)

    if side == "buy":
        cost = quantity * price
        if cost > cash + _EPSILON:
            raise TradeError(
                f"Insufficient cash: need ${cost:,.2f}, have ${cash:,.2f}."
            )
        old_qty = float(existing["quantity"]) if existing else 0.0
        old_avg = float(existing["avg_cost"]) if existing else 0.0
        new_qty = old_qty + quantity
        new_avg = (old_qty * old_avg + quantity * price) / new_qty
        repo.set_cash_balance(cash - cost)
        repo.upsert_position(ticker, new_qty, new_avg)
    else:  # sell
        owned = float(existing["quantity"]) if existing else 0.0
        if quantity > owned + _EPSILON:
            raise TradeError(
                f"Insufficient shares: trying to sell {quantity} of {ticker}, own {owned}."
            )
        proceeds = quantity * price
        remaining = owned - quantity
        repo.set_cash_balance(cash + proceeds)
        if remaining <= _EPSILON:
            repo.delete_position(ticker)
        else:
            # avg_cost is unchanged on a sell
            repo.upsert_position(ticker, remaining, float(existing["avg_cost"]))

    trade = repo.insert_trade(ticker, side, quantity, price)
    # Snapshot the new total value immediately after the trade.
    repo.insert_snapshot(build_portfolio()["total_value"])
    return trade
