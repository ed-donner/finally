"""Portfolio business logic: trade execution, portfolio queries, history."""

from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite

from app.market.cache import PriceCache


async def execute_trade(
    db: aiosqlite.Connection,
    price_cache: PriceCache,
    ticker: str,
    side: str,
    quantity: float,
) -> dict:
    """Execute a market order (buy or sell) with atomic transaction.

    Returns dict with ticker, side, quantity, price, total.
    Raises ValueError for insufficient cash, insufficient shares, or missing price.
    """
    current_price = price_cache.get_price(ticker)
    if current_price is None:
        raise ValueError(f"No price available for {ticker}")

    cost = round(current_price * quantity, 2)
    now = datetime.now(timezone.utc).isoformat()

    try:
        await db.execute("BEGIN")

        if side == "buy":
            row = await db.execute_fetchall(
                "SELECT cash_balance FROM users_profile WHERE id = ?", ("default",)
            )
            cash = row[0][0]
            if cash < cost:
                raise ValueError(
                    f"Insufficient cash: need ${cost:.2f}, have ${cash:.2f}"
                )

            await db.execute(
                "UPDATE users_profile SET cash_balance = cash_balance - ? WHERE id = ?",
                (cost, "default"),
            )

            await db.execute(
                """INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at)
                VALUES (?, 'default', ?, ?, ?, ?)
                ON CONFLICT(user_id, ticker) DO UPDATE SET
                    quantity = positions.quantity + excluded.quantity,
                    avg_cost = (positions.avg_cost * positions.quantity + excluded.avg_cost * excluded.quantity)
                              / (positions.quantity + excluded.quantity),
                    updated_at = excluded.updated_at""",
                (str(uuid4()), ticker, quantity, current_price, now),
            )

        else:  # sell
            row = await db.execute_fetchall(
                "SELECT quantity FROM positions WHERE user_id = ? AND ticker = ?",
                ("default", ticker),
            )
            if not row:
                raise ValueError(f"Insufficient shares: no position in {ticker}")

            owned_qty = row[0][0]
            if owned_qty < quantity:
                raise ValueError(
                    f"Insufficient shares: need {quantity}, have {owned_qty}"
                )

            await db.execute(
                "UPDATE users_profile SET cash_balance = cash_balance + ? WHERE id = ?",
                (cost, "default"),
            )

            new_qty = owned_qty - quantity
            if new_qty < 0.0001:
                await db.execute(
                    "DELETE FROM positions WHERE user_id = ? AND ticker = ?",
                    ("default", ticker),
                )
            else:
                await db.execute(
                    "UPDATE positions SET quantity = ?, updated_at = ? WHERE user_id = ? AND ticker = ?",
                    (new_qty, now, "default", ticker),
                )

        # Record trade in append-only log
        await db.execute(
            "INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid4()), "default", ticker, side, quantity, current_price, now),
        )

        await db.execute("COMMIT")

    except Exception:
        await db.execute("ROLLBACK")
        raise

    return {
        "ticker": ticker,
        "side": side,
        "quantity": quantity,
        "price": current_price,
        "total": cost,
    }


async def get_portfolio(db: aiosqlite.Connection, price_cache: PriceCache) -> dict:
    """Get current portfolio state with live prices and P&L.

    Returns dict with cash_balance, positions list, and total_value.
    Falls back to avg_cost if price_cache has no current price for a ticker.
    """
    row = await db.execute_fetchall(
        "SELECT cash_balance FROM users_profile WHERE id = ?", ("default",)
    )
    cash_balance = row[0][0]

    rows = await db.execute_fetchall(
        "SELECT ticker, quantity, avg_cost FROM positions WHERE user_id = ?",
        ("default",),
    )

    positions = []
    total_market_value = 0.0

    for r in rows:
        ticker, qty, avg_cost = r[0], r[1], r[2]
        current_price = price_cache.get_price(ticker)
        if current_price is None:
            current_price = avg_cost

        market_value = round(current_price * qty, 2)
        cost_basis = round(avg_cost * qty, 2)
        unrealized_pnl = round(market_value - cost_basis, 2)
        unrealized_pnl_percent = round((unrealized_pnl / cost_basis) * 100, 2) if cost_basis else 0.0

        positions.append({
            "ticker": ticker,
            "quantity": qty,
            "avg_cost": avg_cost,
            "current_price": current_price,
            "market_value": market_value,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_percent": unrealized_pnl_percent,
        })
        total_market_value += market_value

    return {
        "cash_balance": cash_balance,
        "positions": positions,
        "total_value": round(cash_balance + total_market_value, 2),
    }


async def get_portfolio_history(db: aiosqlite.Connection) -> dict:
    """Get portfolio value snapshots ordered chronologically.

    Returns dict with snapshots list (each: total_value, recorded_at).
    """
    rows = await db.execute_fetchall(
        "SELECT total_value, recorded_at FROM portfolio_snapshots WHERE user_id = ? ORDER BY recorded_at ASC",
        ("default",),
    )

    return {
        "snapshots": [
            {"total_value": r[0], "recorded_at": r[1]}
            for r in rows
        ],
    }
