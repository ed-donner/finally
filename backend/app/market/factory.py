"""Factory for creating market data sources."""

from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, raw, default)
        return default


def create_market_data_source(
    price_cache: PriceCache,
    initial_prices: dict[str, float] | None = None,
) -> MarketDataSource:
    """Pick the market data source from the environment.

    - MASSIVE_API_KEY set and non-blank -> MassiveDataSource (mode auto-detected)
    - otherwise                         -> SimulatorDataSource

    initial_prices (e.g. avg_cost of held positions) only affects the simulator.
    Returns an unstarted source; the caller must `await source.start(tickers)`.
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        interval = max(_float_env("MASSIVE_POLL_INTERVAL", 15.0), 1.0)
        logger.info("Market data source: Massive API (poll %.0fs)", interval)
        return MassiveDataSource(api_key=api_key, price_cache=price_cache, poll_interval=interval)
    logger.info("Market data source: GBM simulator")
    return SimulatorDataSource(price_cache=price_cache, initial_prices=initial_prices)
