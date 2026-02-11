"""Watchlist subsystem for FinAlly.

Public API:
    create_watchlist_router - FastAPI router factory for watchlist endpoints
    get_watchlist           - Fetch all watchlist tickers from DB
    add_ticker              - Add a ticker to the watchlist
    remove_ticker           - Remove a ticker from the watchlist
"""

from .service import add_ticker, get_watchlist, remove_ticker

__all__ = [
    "get_watchlist",
    "add_ticker",
    "remove_ticker",
]
