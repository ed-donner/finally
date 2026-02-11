"""Portfolio service: trade execution, positions, P&L."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlite3 import Connection

from app.market import PriceCache


@dataclass
class TradeResult:
    ticker: str
    side: str
    quantity: float
    price: float
    total_cost: float
    cash_after: float


class PortfolioService:
    """Manages portfolio operations against SQLite + PriceCache."""

    def __init__(self, db: Connection, price_cache: PriceCache, user_id: str = "default") -> None:
        self.db = db
        self.price_cache = price_cache
        self.user_id = user_id

    def get_portfolio(self) -> dict:
        """Get full portfolio: cash, positions with live P&L, total value."""
        cash = self._get_cash()
        positions = self._get_positions_with_pnl()
        positions_value = sum(p["market_value"] for p in positions)
        return {
            "cash": cash,
            "positions": positions,
            "total_value": round(cash + positions_value, 2),
        }

    def execute_trade(self, ticker: str, side: str, quantity: float) -> TradeResult:
        """Execute a market order. Raises ValueError on invalid trades."""
        if quantity <= 0:
            raise ValueError("Quantity must be positive")
        if side not in ("buy", "sell"):
            raise ValueError("Side must be 'buy' or 'sell'")

        price = self.price_cache.get_price(ticker)
        if price is None:
            raise ValueError(f"No price available for {ticker}")

        if side == "buy":
            return self._execute_buy(ticker, quantity, price)
        return self._execute_sell(ticker, quantity, price)

    def get_history(self) -> list[dict]:
        """Get portfolio value snapshots for P&L chart."""
        rows = self.db.execute(
            "SELECT total_value, recorded_at FROM portfolio_snapshots "
            "WHERE user_id = ? ORDER BY recorded_at",
            (self.user_id,),
        ).fetchall()
        return [{"total_value": r["total_value"], "recorded_at": r["recorded_at"]} for r in rows]

    def record_snapshot(self) -> None:
        """Record current portfolio value as a snapshot."""
        portfolio = self.get_portfolio()
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), self.user_id, portfolio["total_value"], now),
        )
        self.db.commit()

    def _get_cash(self) -> float:
        row = self.db.execute(
            "SELECT cash_balance FROM users_profile WHERE id = ?", (self.user_id,)
        ).fetchone()
        return row["cash_balance"] if row else 10000.0

    def _get_positions_with_pnl(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT ticker, quantity, avg_cost FROM positions WHERE user_id = ? AND quantity > 0",
            (self.user_id,),
        ).fetchall()
        positions = []
        for r in rows:
            current_price = self.price_cache.get_price(r["ticker"]) or r["avg_cost"]
            market_value = r["quantity"] * current_price
            cost_basis = r["quantity"] * r["avg_cost"]
            unrealized_pnl = market_value - cost_basis
            pnl_percent = (unrealized_pnl / cost_basis * 100) if cost_basis else 0
            positions.append({
                "ticker": r["ticker"],
                "quantity": r["quantity"],
                "avg_cost": round(r["avg_cost"], 2),
                "current_price": round(current_price, 2),
                "market_value": round(market_value, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "pnl_percent": round(pnl_percent, 2),
            })
        return positions

    def _execute_buy(self, ticker: str, quantity: float, price: float) -> TradeResult:
        total_cost = round(quantity * price, 2)
        cash = self._get_cash()
        if total_cost > cash:
            raise ValueError(
                f"Insufficient cash: need ${total_cost:.2f}, have ${cash:.2f}"
            )

        now = datetime.now(timezone.utc).isoformat()

        # Update cash
        self.db.execute(
            "UPDATE users_profile SET cash_balance = cash_balance - ? WHERE id = ?",
            (total_cost, self.user_id),
        )

        # Update or create position
        existing = self.db.execute(
            "SELECT quantity, avg_cost FROM positions WHERE user_id = ? AND ticker = ?",
            (self.user_id, ticker),
        ).fetchone()

        if existing:
            new_qty = existing["quantity"] + quantity
            new_avg = (existing["quantity"] * existing["avg_cost"] + total_cost) / new_qty
            self.db.execute(
                "UPDATE positions SET quantity = ?, avg_cost = ?, updated_at = ? "
                "WHERE user_id = ? AND ticker = ?",
                (new_qty, new_avg, now, self.user_id, ticker),
            )
        else:
            self.db.execute(
                "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), self.user_id, ticker, quantity, price, now),
            )

        # Record trade
        self.db.execute(
            "INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), self.user_id, ticker, "buy", quantity, price, now),
        )
        self.db.commit()

        cash_after = self._get_cash()
        return TradeResult(ticker, "buy", quantity, price, total_cost, cash_after)

    def _execute_sell(self, ticker: str, quantity: float, price: float) -> TradeResult:
        existing = self.db.execute(
            "SELECT quantity FROM positions WHERE user_id = ? AND ticker = ?",
            (self.user_id, ticker),
        ).fetchone()

        if not existing or existing["quantity"] < quantity:
            held = existing["quantity"] if existing else 0
            raise ValueError(
                f"Insufficient shares: want to sell {quantity}, hold {held}"
            )

        now = datetime.now(timezone.utc).isoformat()
        proceeds = round(quantity * price, 2)

        # Update cash
        self.db.execute(
            "UPDATE users_profile SET cash_balance = cash_balance + ? WHERE id = ?",
            (proceeds, self.user_id),
        )

        # Update position
        new_qty = existing["quantity"] - quantity
        if new_qty > 0:
            self.db.execute(
                "UPDATE positions SET quantity = ?, updated_at = ? "
                "WHERE user_id = ? AND ticker = ?",
                (new_qty, now, self.user_id, ticker),
            )
        else:
            self.db.execute(
                "DELETE FROM positions WHERE user_id = ? AND ticker = ?",
                (self.user_id, ticker),
            )

        # Record trade
        self.db.execute(
            "INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), self.user_id, ticker, "sell", quantity, price, now),
        )
        self.db.commit()

        cash_after = self._get_cash()
        return TradeResult(ticker, "sell", quantity, price, proceeds, cash_after)
