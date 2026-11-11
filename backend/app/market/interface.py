"""Abstract interface for market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable


def normalize_ticker(ticker: str) -> str:
    """Canonical ticker form used everywhere (DB, cache, sources): stripped, upper-case."""
    return ticker.strip().upper()


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push PriceUpdates into a shared PriceCache on their own schedule.
    Consumers read the cache, never the source.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL"])
        await source.add_ticker("TSLA")
        await source.set_tickers({"AAPL", "TSLA"})   # reconcile with DB state
        await source.stop()

    Contract (checked by tests/market/test_interface.py for every implementation):
        C1 all public methods normalize tickers with normalize_ticker
        C2 after start() returns, the cache is seeded (simulator) or one fetch was attempted
        C3 add_ticker is idempotent
        C4 remove_ticker is idempotent and evicts the ticker from the cache
        C5 stop() is idempotent and safe before start()
        C6 background exceptions are logged, never propagated out of the task
        C7 no method blocks the event loop
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing prices. Call once."""

    @abstractmethod
    async def stop(self) -> None:
        """Cancel background work. Idempotent; safe before start()."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Track a ticker. No-op if already tracked."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker and evict it from the cache. No-op if absent."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Currently tracked tickers."""

    async def set_tickers(self, tickers: Iterable[str]) -> None:
        """Reconcile the tracked set with `tickers` (adds missing, removes extras)."""
        wanted = {normalize_ticker(t) for t in tickers if t and t.strip()}
        current = set(self.get_tickers())
        for t in sorted(wanted - current):
            await self.add_ticker(t)
        for t in sorted(current - wanted):
            await self.remove_ticker(t)
