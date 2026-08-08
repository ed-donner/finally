from __future__ import annotations

from abc import ABC, abstractmethod

from .types import Bar


class MarketDataSource(ABC):
    """Produces prices (and history) for tickers. Two methods to implement."""

    #: How often the feed loop should ask this source for fresh prices.
    poll_interval_seconds: float

    @abstractmethod
    async def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """Return {ticker: price} for as many requested tickers as available.
        Missing tickers are omitted; the feed keeps their last cached value."""
        ...

    @abstractmethod
    async def get_history(self, ticker: str, days: int = 90) -> list[Bar]:
        """Return up to `days` daily OHLC bars, oldest first, for the detail
        chart. Empty list if unavailable."""
        ...

    async def aclose(self) -> None:
        """Release resources (HTTP client, etc.). Default: no-op."""
        return None
