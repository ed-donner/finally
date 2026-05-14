from abc import ABC, abstractmethod
from .models import PriceUpdate, DailyBar


class MarketDataSource(ABC):
    """Abstract base class for all market data sources.

    Concrete implementations: MassiveClient, MarketSimulator.
    Neither should be used directly outside backend/app/market/.
    All external code reads from PriceCache.
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Start the background polling/simulation loop.

        Args:
            tickers: Initial list of ticker symbols to track.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down the background task."""
        ...

    @abstractmethod
    def add_ticker(self, ticker: str) -> None:
        """Register a new ticker to be tracked on the next poll cycle."""
        ...

    @abstractmethod
    def remove_ticker(self, ticker: str) -> None:
        """Deregister a ticker. Its entry remains in cache until overwritten."""
        ...

    @abstractmethod
    async def get_daily_bars(
        self, ticker: str, from_date: str, to_date: str
    ) -> list[DailyBar]:
        """Fetch historical daily OHLCV bars.

        Args:
            ticker: Ticker symbol.
            from_date: ISO date string YYYY-MM-DD.
            to_date: ISO date string YYYY-MM-DD.

        Returns:
            List of DailyBar sorted ascending by date.
        """
        ...
