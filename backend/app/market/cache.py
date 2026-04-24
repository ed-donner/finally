"""Thread-safe in-memory price cache."""

from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

    def update(
        self,
        ticker: str,
        price: float,
        timestamp: float | None = None,
        open_price: float | None = None,
    ) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        open_price is only used on the first update for a ticker; ignored on
        subsequent calls so the session-start baseline is never overwritten.
        If not provided on the first update, price is used as the baseline.
        """
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)

            if prev:
                prev_price = prev.price
                effective_open = prev.open_price  # Never overwrite
            else:
                prev_price = price
                effective_open = open_price if open_price is not None else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                prev_price=round(prev_price, 2),
                open_price=round(effective_open, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Get the latest price for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: get just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache (e.g., when removed from watchlist)."""
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Current version counter. Useful for SSE change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
