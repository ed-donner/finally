"""Watchlist service: CRUD plus keeping the market data source in sync with DB state."""

from __future__ import annotations

from app import db
from app.market import MarketDataSource, PriceCache

from .portfolio import validate_ticker


class WatchlistError(ValueError):
    """Base class for watchlist failures. ``str(exc)`` is user-facing."""


class InvalidTickerError(WatchlistError):
    """Ticker failed validation (HTTP 400)."""


class DuplicateTickerError(WatchlistError):
    """Ticker already in the watchlist (HTTP 409)."""


def watchlist_item(ticker: str, price_cache: PriceCache) -> dict:
    """One watchlist entry; price fields are None if the cache has no price yet."""
    update = price_cache.get(ticker)
    if update is None:
        return {
            "ticker": ticker,
            "price": None,
            "previous_price": None,
            "change_percent": None,
            "day_change_percent": None,
            "direction": None,
            "timestamp": None,
        }
    return {
        "ticker": ticker,
        "price": update.price,
        "previous_price": update.previous_price,
        "change_percent": update.change_percent,
        "day_change_percent": update.day_change_percent,
        "direction": update.direction,
        "timestamp": update.timestamp,
    }


def get_watchlist_with_prices(price_cache: PriceCache) -> list[dict]:
    return [watchlist_item(t, price_cache) for t in db.get_watchlist()]


def tracked_tickers() -> set[str]:
    """Tickers the market source should track: watchlist ∪ held positions."""
    return set(db.get_watchlist()) | {p["ticker"] for p in db.get_positions()}


async def sync_tracked_tickers(source: MarketDataSource) -> None:
    """Reconcile the market source with watchlist ∪ held positions."""
    await source.set_tickers(tracked_tickers())


async def add_ticker(ticker: str, source: MarketDataSource) -> dict:
    """Add a ticker to the watchlist and start tracking it.

    Returns ``{"ticker": ...}``. Raises InvalidTickerError / DuplicateTickerError
    (both ValueError subclasses).
    """
    try:
        norm = validate_ticker(ticker)
    except ValueError as exc:
        raise InvalidTickerError(str(exc)) from None
    if not db.add_watchlist_ticker(norm):
        raise DuplicateTickerError(f"{norm} is already in the watchlist")
    await source.add_ticker(norm)
    return {"ticker": norm}


async def remove_ticker(ticker: str, source: MarketDataSource) -> bool:
    """Remove a ticker from the watchlist. Returns False if it wasn't present.

    The ticker keeps being tracked if a position is still held in it.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        return False
    removed = db.remove_watchlist_ticker(ticker)
    if removed:
        await sync_tracked_tickers(source)
    return removed
