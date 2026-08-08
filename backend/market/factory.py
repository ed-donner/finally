from __future__ import annotations

from config import settings

from .massive import MassiveSource
from .simulator import SimulatedSource
from .source import MarketDataSource


def make_source() -> MarketDataSource:
    """Massive if MASSIVE_API_KEY is set and non-empty, else the simulator."""
    if settings.use_massive:
        return MassiveSource(
            settings.massive_api_key,
            poll_interval_seconds=settings.massive_poll_seconds,
        )
    return SimulatedSource(seed=settings.sim_seed)
