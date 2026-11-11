"""Thread-safe in-memory price cache."""

from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate

_UNSET: object = object()


class PriceCache:
    """Latest PriceUpdate per ticker.

    Writers: exactly one MarketDataSource (event-loop thread or a to_thread worker).
    Readers: SSE, trade execution, portfolio valuation, snapshot task, LLM context.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version = 0  # bumped on every mutation (update AND remove)

    def update(
        self,
        ticker: str,
        price: float,
        timestamp: float | None = None,
        reference_price: float | None | object = _UNSET,
    ) -> PriceUpdate:
        """Record a new price. previous_price/direction are derived from the prior entry.

        If this is the first update for the ticker, previous_price == price (direction 'flat').
        reference_price: omit to carry forward the existing value; pass None to clear it.
        """
        with self._lock:
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price
            if reference_price is _UNSET:
                reference_price = prev.reference_price if prev else None
            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=timestamp or time.time(),
                reference_price=None if reference_price is None else round(reference_price, 2),
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Latest update for a ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: just the price, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        """Consistent snapshot of all prices (shallow copy)."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        """Evict a ticker. Bumps the version so SSE pushes the shrunken set promptly."""
        with self._lock:
            if self._prices.pop(ticker, None) is not None:
                self._version += 1

    @property
    def version(self) -> int:
        """Monotonic counter, incremented on every mutation. Used for SSE change detection."""
        with self._lock:
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
