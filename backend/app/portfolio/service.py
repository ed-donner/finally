"""Trade execution and portfolio valuation.

Pure-ish domain logic: takes a PriceCache and operates on the DB. No HTTP.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

from .. import db
from ..market import PriceCache
from ..validation import normalize_ticker, normalize_trade_quantity

Side = Literal["buy", "sell"]
MAX_TRADE_PRICE_AGE_SECONDS = 300

logger = logging.getLogger(__name__)


class TradeError(ValueError):
    """Raised when a trade fails validation (insufficient cash, no shares, etc.)."""


@dataclass(frozen=True)
class TradeResult:
    ticker: str
    side: Side
    quantity: float
    price: float
    cash_balance_after: float
    position_quantity_after: float
    executed_at: str


@dataclass(frozen=True)
class PositionView:
    ticker: str
    quantity: float
    avg_cost: float
    current_price: float | None
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_percent: float


@dataclass(frozen=True)
class PortfolioView:
    cash_balance: float
    positions: list[PositionView]
    market_value: float  # sum of position market values (excludes cash)
    total_value: float  # cash + market_value
    unrealized_pnl: float
    unrealized_pnl_percent: float  # vs total cost basis


def _require_price(cache: PriceCache, ticker: str) -> float:
    update = cache.get(ticker)
    if update is None or update.price <= 0:
        raise TradeError(f"No live price available for {ticker}")
    if time.time() - update.timestamp > MAX_TRADE_PRICE_AGE_SECONDS:
        raise TradeError(
            f"Live price for {ticker} is stale; wait for a fresh quote before trading"
        )
    return update.price


def execute_trade(
    cache: PriceCache,
    ticker: str,
    side: Side,
    quantity: float,
) -> TradeResult:
    """Execute a market order at the current cached price.

    Validation:
      - quantity > 0
      - side in {"buy", "sell"}
      - live price available
      - buy: sufficient cash
      - sell: sufficient shares

    On success: updates positions + cash, appends a row to `trades`.
    """
    try:
        ticker = normalize_ticker(ticker)
    except ValueError as e:
        raise TradeError(str(e)) from e
    if side not in ("buy", "sell"):
        raise TradeError(f"side must be 'buy' or 'sell', got {side!r}")
    try:
        quantity = normalize_trade_quantity(quantity)
    except ValueError as e:
        raise TradeError(str(e)) from e

    price = _require_price(cache, ticker)
    cash = db.profile.get_cash_balance()
    existing = db.positions.get_position(ticker)

    if side == "buy":
        cost = quantity * price
        if cost > cash + 1e-9:
            raise TradeError(
                f"Insufficient cash: need ${cost:,.2f}, have ${cash:,.2f}"
            )
        new_cash = cash - cost
        if existing is None:
            new_qty = quantity
            new_avg = price
        else:
            new_qty = existing.quantity + quantity
            new_avg = (
                existing.quantity * existing.avg_cost + quantity * price
            ) / new_qty
        db.positions.upsert_position(ticker, new_qty, new_avg)
    else:  # sell
        if existing is None or existing.quantity < quantity - 1e-9:
            have = existing.quantity if existing else 0.0
            raise TradeError(
                f"Insufficient shares of {ticker}: need {quantity}, have {have}"
            )
        new_cash = cash + quantity * price
        new_qty = existing.quantity - quantity
        if new_qty < 1e-9:
            db.positions.delete_position(ticker)
            new_qty = 0.0
        else:
            db.positions.upsert_position(ticker, new_qty, existing.avg_cost)

    db.profile.set_cash_balance(new_cash)
    trade = db.trades.record_trade(ticker, side, quantity, price)
    try:
        db.snapshots.record_snapshot(total_value(cache))
    except Exception:
        logger.exception("failed to record post-trade snapshot")

    return TradeResult(
        ticker=ticker,
        side=side,
        quantity=quantity,
        price=price,
        cash_balance_after=new_cash,
        position_quantity_after=new_qty,
        executed_at=trade.executed_at,
    )


def get_portfolio_view(cache: PriceCache) -> PortfolioView:
    """Compose current cash, positions, and live valuations into a single view."""
    cash = db.profile.get_cash_balance()
    raw_positions = db.positions.list_positions()

    views: list[PositionView] = []
    market_value = 0.0
    total_cost = 0.0
    total_unrealized = 0.0
    for p in raw_positions:
        current_price = cache.get_price(p.ticker)
        cost_basis = p.quantity * p.avg_cost
        if current_price is not None:
            mv = p.quantity * current_price
            pnl = mv - cost_basis
            pnl_pct = (pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0
        else:
            mv = cost_basis  # fall back to cost basis if no price yet
            pnl = 0.0
            pnl_pct = 0.0
        market_value += mv
        total_cost += cost_basis
        total_unrealized += pnl
        views.append(
            PositionView(
                ticker=p.ticker,
                quantity=p.quantity,
                avg_cost=p.avg_cost,
                current_price=current_price,
                market_value=mv,
                unrealized_pnl=pnl,
                unrealized_pnl_percent=pnl_pct,
            )
        )

    total = cash + market_value
    pnl_pct = (total_unrealized / total_cost * 100.0) if total_cost > 0 else 0.0
    return PortfolioView(
        cash_balance=cash,
        positions=views,
        market_value=market_value,
        total_value=total,
        unrealized_pnl=total_unrealized,
        unrealized_pnl_percent=pnl_pct,
    )


def total_value(cache: PriceCache) -> float:
    """Cheap version: just the total. Used by the snapshot background task."""
    cash = db.profile.get_cash_balance()
    mv = 0.0
    for p in db.positions.list_positions():
        price = cache.get_price(p.ticker)
        mv += p.quantity * (price if price is not None else p.avg_cost)
    return cash + mv
