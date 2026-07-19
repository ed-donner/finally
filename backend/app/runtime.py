"""Process-wide runtime handles for the shared market data objects.

``main.py`` creates a single :class:`PriceCache` and market source in its
lifespan and registers them here so services, routers, and the chat handler
can reach them without threading ``app.state`` through every call.
"""

from __future__ import annotations

from app.market import MarketDataSource, PriceCache

_price_cache: PriceCache | None = None
_market_source: MarketDataSource | None = None


def set_runtime(price_cache: PriceCache, market_source: MarketDataSource) -> None:
    global _price_cache, _market_source
    _price_cache = price_cache
    _market_source = market_source


def get_price_cache() -> PriceCache:
    if _price_cache is None:
        raise RuntimeError("Price cache not initialized — call set_runtime() at startup.")
    return _price_cache


def get_market_source() -> MarketDataSource:
    if _market_source is None:
        raise RuntimeError("Market source not initialized — call set_runtime() at startup.")
    return _market_source


def reset_runtime() -> None:
    """Test helper: clear registered handles."""
    global _price_cache, _market_source
    _price_cache = None
    _market_source = None
