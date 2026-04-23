"""Application state container shared across request handlers.

Held on `app.state.finally_state` after lifespan startup. Routes resolve via
the `get_state` dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from .market import MarketDataSource, PriceCache


@dataclass
class AppState:
    price_cache: PriceCache
    market_source: MarketDataSource


def get_state(request: Request) -> AppState:
    """FastAPI dependency to resolve AppState from the request scope."""
    state: AppState = request.app.state.finally_state
    return state
