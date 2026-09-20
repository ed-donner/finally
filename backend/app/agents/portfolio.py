"""Portfolio Agent: the only writer of cash, positions and trades."""

from __future__ import annotations

from ..db import Database
from ..market import PriceCache
from .base import EPS, USER_ID, BaseAgent, correlation_id, new_id, now_iso
from .models import (
    PortfolioSummary,
    PositionView,
    RiskVerdict,
    Snapshot,
    TradeRequest,
    TradeResult,
)


class TradeError(Exception):
    """A trade that cannot be filled mechanically (raised inside the transaction -> rollback)."""


class PortfolioAgent(BaseAgent):
    agent_id = "portfolio"

    def __init__(self, db: Database, cache: PriceCache) -> None:
        super().__init__(db)
        self.cache = cache

    # ------------------------------------------------------------------ trading

    def execute_trade(self, req: TradeRequest, verdict: RiskVerdict) -> TradeResult:
        """Fill a market order at the cached price. Atomic: any failure rolls everything back."""
        with self.timed() as elapsed:
            result = self._execute(req, verdict)
            self.log(
                "execute_trade",
                "success" if result.ok else "rejected",
                input=req,
                output=result,
                error=result.error,
                duration_ms=elapsed(),
            )
        return result

    def _execute(self, req: TradeRequest, verdict: RiskVerdict) -> TradeResult:
        def fail(msg: str) -> TradeResult:
            return TradeResult(
                ok=False, ticker=req.ticker, side=req.side, quantity=req.quantity, error=msg
            )

        # Defense in depth: this agent never decides what is *allowed*, but it refuses to act
        # without an approved Risk verdict.
        if not verdict.approved:
            return fail("; ".join(verdict.reasons) or "trade not approved by risk agent")

        # Market order: fill instantly at the latest cached price.
        price = self.cache.get_price(req.ticker)
        if price is None or price <= 0:
            return fail(f"no price available for {req.ticker}")

        try:
            # One atomic unit: cash, position, trade log, aggregates and the P&L-chart snapshot
            # all commit together or not at all.
            with self.db.transaction():
                # Re-read inside the transaction (Risk validated earlier; state may have moved).
                cash = self.db.query_one(
                    "SELECT cash_balance FROM portfolio_state WHERE user_id=?", (USER_ID,)
                )["cash_balance"]
                pos = self.db.query_one(
                    "SELECT id, quantity, avg_cost FROM positions WHERE user_id=? AND ticker=?",
                    (USER_ID, req.ticker),
                )
                ts = now_iso()
                apply = self._apply_buy if req.side == "buy" else self._apply_sell
                new_cash, realized = apply(pos, req, price, cash, ts)

                self.db.execute(
                    "UPDATE portfolio_state SET cash_balance=? WHERE user_id=?",
                    (new_cash, USER_ID),
                )
                trade_id = new_id()
                self.db.execute(
                    "INSERT INTO trades (id, user_id, ticker, side, quantity, price, realized_pnl,"
                    " timestamp, agent_id, source, correlation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        trade_id,
                        USER_ID,
                        req.ticker,
                        req.side,
                        req.quantity,
                        price,
                        realized,
                        ts,
                        self.agent_id,
                        req.source,
                        correlation_id.get() or None,
                    ),
                )
                # Value the book once, persist it, and record the chart point from that same value.
                self.record_snapshot(self.get_summary())
        except TradeError as e:
            # The transaction context manager already rolled back; report it as a failed trade.
            return fail(str(e))

        return TradeResult(
            ok=True,
            trade_id=trade_id,
            ticker=req.ticker,
            side=req.side,
            quantity=req.quantity,
            price=price,
            realized_pnl=realized,
            warnings=verdict.warnings,
        )

    def _apply_buy(
        self, pos: dict | None, req: TradeRequest, price: float, cash: float, ts: str
    ) -> tuple[float, float | None]:
        """Upsert the position for a buy. Returns (new_cash, realized_pnl=None)."""
        cost = req.quantity * price
        if cost > cash + EPS:
            raise TradeError(f"insufficient cash: need ${cost:,.2f}, have ${cash:,.2f}")
        if pos:
            # Weighted-average cost across the old and new lots.
            new_qty = pos["quantity"] + req.quantity
            new_avg = (pos["quantity"] * pos["avg_cost"] + cost) / new_qty
            self.db.execute(
                "UPDATE positions SET quantity=?, avg_cost=?, current_price=?, updated_at=?"
                " WHERE id=?",
                (new_qty, new_avg, price, ts, pos["id"]),
            )
        else:
            self.db.execute(
                "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, current_price,"
                " updated_at) VALUES (?,?,?,?,?,?,?)",
                (new_id(), USER_ID, req.ticker, req.quantity, price, price, ts),
            )
        # Clamp at 0 so float dust cannot violate the cash_balance >= 0 CHECK constraint.
        return max(0.0, cash - cost), None

    def _apply_sell(
        self, pos: dict | None, req: TradeRequest, price: float, cash: float, ts: str
    ) -> tuple[float, float]:
        """Reduce or close the position for a sell. Returns (new_cash, realized_pnl)."""
        if not pos or req.quantity > pos["quantity"] + EPS:
            held = pos["quantity"] if pos else 0
            raise TradeError(
                f"insufficient shares: trying to sell {req.quantity:g} {req.ticker}, hold {held:g}"
            )
        # Guard against selling a hair more than held because of float tolerance.
        qty = min(req.quantity, pos["quantity"])
        remaining = pos["quantity"] - qty
        if remaining < EPS:
            # A fully closed position is deleted (positions.quantity must be > 0).
            self.db.execute("DELETE FROM positions WHERE id=?", (pos["id"],))
        else:
            self.db.execute(
                "UPDATE positions SET quantity=?, current_price=?, updated_at=? WHERE id=?",
                (remaining, price, ts, pos["id"]),
            )
        # Realized P&L = (sale price - average cost) * shares sold; avg cost is unchanged.
        return cash + qty * price, (price - pos["avg_cost"]) * qty

    # ------------------------------------------------------------------ valuation

    def get_summary(self) -> PortfolioSummary:
        """Value the portfolio at current prices. Read-only: no transaction, no writes."""
        state = self.db.query_one("SELECT * FROM portfolio_state WHERE user_id=?", (USER_ID,))
        rows = self.db.query("SELECT * FROM positions WHERE user_id=? ORDER BY ticker", (USER_ID,))
        views: list[PositionView] = []
        for r in rows:
            # Price fallback: live cache -> last stored price -> cost basis, so a ticker that lost
            # its live feed does not make the portfolio value collapse to zero.
            price = self.cache.get_price(r["ticker"]) or r["current_price"] or r["avg_cost"]
            value = r["quantity"] * price
            basis = r["quantity"] * r["avg_cost"]
            views.append(
                PositionView(
                    ticker=r["ticker"],
                    quantity=r["quantity"],
                    avg_cost=r["avg_cost"],
                    current_price=price,
                    market_value=value,
                    unrealized_pnl=value - basis,
                    pnl_percent=((value - basis) / basis * 100) if basis else 0.0,
                )
            )
        positions_value = sum(v.market_value for v in views)
        total = state["cash_balance"] + positions_value
        # Realized P&L is derived from the append-only trade log rather than stored incrementally.
        realized = self.db.query_one(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS r FROM trades WHERE user_id=?", (USER_ID,)
        )["r"]
        return PortfolioSummary(
            cash_balance=state["cash_balance"],
            positions=views,
            positions_value=positions_value,
            total_value=total,
            starting_cash=state["starting_cash"],
            unrealized_pnl=sum(v.unrealized_pnl for v in views),
            realized_pnl=realized,
            total_pnl=total - state["starting_cash"],
        )

    # ------------------------------------------------------------------ persistence / snapshots

    def record_snapshot(self, summary: PortfolioSummary | None = None) -> Snapshot:
        """Persist the cached aggregates and add a P&L-chart point (30 s loop and after each trade).

        Pass an already-computed summary to avoid valuing the book twice.
        """
        s = summary or self.get_summary()
        ts = now_iso()
        with self.db.transaction():
            # Cached aggregates (positions.current_price is also the fallback price above).
            for p in s.positions:
                self.db.execute(
                    "UPDATE positions SET current_price=? WHERE user_id=? AND ticker=?",
                    (p.current_price, USER_ID, p.ticker),
                )
            self.db.execute(
                "UPDATE portfolio_state SET total_value=?, unrealized_pnl=?, realized_pnl=?,"
                " total_pnl=?, updated_at=? WHERE user_id=?",
                (s.total_value, s.unrealized_pnl, s.realized_pnl, s.total_pnl, ts, USER_ID),
            )
            self.db.execute(
                "INSERT INTO portfolio_snapshots (id, user_id, total_value, cash_balance,"
                " positions_value, total_pnl, recorded_at) VALUES (?,?,?,?,?,?,?)",
                (
                    new_id(),
                    USER_ID,
                    s.total_value,
                    s.cash_balance,
                    s.positions_value,
                    s.total_pnl,
                    ts,
                ),
            )
        return Snapshot(
            total_value=s.total_value,
            cash_balance=s.cash_balance,
            positions_value=s.positions_value,
            total_pnl=s.total_pnl,
            recorded_at=ts,
        )

    # Returns the newest `limit` snapshots in chronological order (what a line chart expects).
    def get_history(self, limit: int = 1000) -> list[Snapshot]:
        rows = self.db.query(
            "SELECT total_value, cash_balance, positions_value, total_pnl, recorded_at FROM ("
            " SELECT * FROM portfolio_snapshots WHERE user_id=? ORDER BY recorded_at DESC LIMIT ?"
            ") ORDER BY recorded_at ASC",
            (USER_ID, limit),
        )
        return [Snapshot(**r) for r in rows]
