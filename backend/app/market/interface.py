"""Abstract interface for market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod


def normalize_ticker(ticker: str) -> str:
    """Canonical ticker form used everywhere: stripped and upper-cased.

    Both data sources and the cache key on this so ``"aapl"``, ``" AAPL "`` and
    ``"AAPL"`` all refer to the same instrument (PLAN open-question #3).
    """
    return ticker.strip().upper()


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        # ... app runs ...
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        # ... app shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task that periodically writes to the PriceCache.
        Must be called exactly once. Calling start() twice is undefined behavior.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times. After stop(), the source will not write
        to the cache again.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present.

        The next update cycle will include this ticker.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. No-op if not present.

        Also removes the ticker from the PriceCache.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""

    def health(self) -> dict:
        """Report data-source liveness for the connection indicator / health API.

        Returns at least ``{"healthy": bool, "last_update": float | None}``.
        ``last_update`` is the Unix time of the most recent successful write to
        the cache (``None`` if nothing has been produced yet). The base
        implementation reports healthy with no timestamp; sources that can fail
        upstream (e.g. a polling REST client) override this so a dead feed is
        distinguishable from a merely-quiet one.
        """
        return {"healthy": True, "last_update": None}
