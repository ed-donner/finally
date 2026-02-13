"""Factory for creating market data sources."""

from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment variables.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
    - Otherwise → SimulatorDataSource (GBM simulation)

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        raw_interval = os.environ.get("MASSIVE_POLL_INTERVAL_SECONDS", "0.5").strip()
        raw_stale_seconds = os.environ.get("MASSIVE_STALE_TRADE_SECONDS", "10").strip()
        try:
            poll_interval = float(raw_interval)
        except ValueError:
            poll_interval = 0.5
        try:
            stale_trade_seconds = float(raw_stale_seconds)
        except ValueError:
            stale_trade_seconds = 10.0
        if poll_interval <= 0:
            poll_interval = 0.5
        if stale_trade_seconds < 0:
            stale_trade_seconds = 10.0

        logger.info("Market data source: Massive API (real data, %.3fs poll)", poll_interval)
        return MassiveDataSource(
            api_key=api_key,
            price_cache=price_cache,
            poll_interval=poll_interval,
            stale_trade_seconds=stale_trade_seconds,
        )
    else:
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
