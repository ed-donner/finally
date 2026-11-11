"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time.

    previous_price  - price at the previous update (tick-to-tick; drives the flash colour)
    reference_price - "start of day" price used for the day change:
                        simulator: price when the ticker started being simulated
                        Massive:   previous session's close
                      None when unknown.
    """

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds
    reference_price: float | None = None

    @property
    def change(self) -> float:
        """Absolute price change from the previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from the previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat' relative to the previous update."""
        if self.price > self.previous_price:
            return "up"
        if self.price < self.previous_price:
            return "down"
        return "flat"

    @property
    def day_change(self) -> float | None:
        """Absolute change versus the reference price, or None if unknown."""
        if not self.reference_price:
            return None
        return round(self.price - self.reference_price, 2)

    @property
    def day_change_percent(self) -> float | None:
        """Percentage change versus the reference price, or None if unknown."""
        if not self.reference_price:
            return None
        return round((self.price - self.reference_price) / self.reference_price * 100, 2)

    def to_dict(self) -> dict:
        """JSON-ready dict; this is the per-ticker SSE payload."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
            "reference_price": self.reference_price,
            "day_change": self.day_change,
            "day_change_percent": self.day_change_percent,
        }
