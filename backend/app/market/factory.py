import os
import logging
from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(cache: PriceCache) -> MarketDataSource:
    """Return the appropriate MarketDataSource based on environment config.

    - If MASSIVE_API_KEY is set and non-empty → MassiveClient
    - Otherwise → MarketSimulator

    The poll interval for MassiveClient is read from MASSIVE_POLL_INTERVAL
    (seconds, float). Defaults to 15.0 (safe for the free tier).
    """
    api_key = os.getenv("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveClient
        poll_interval = float(os.getenv("MASSIVE_POLL_INTERVAL", "15.0"))
        logger.info("Using Massive API (poll interval: %.1fs)", poll_interval)
        return MassiveClient(api_key=api_key, cache=cache, poll_interval=poll_interval)

    from .simulator import MarketSimulator
    logger.info("Using market simulator (no MASSIVE_API_KEY set)")
    return MarketSimulator(cache=cache)
