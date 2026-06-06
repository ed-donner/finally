"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time.

    Two reference points are tracked:
      - ``previous_price`` — the immediately preceding tick. Drives the
        green/red flash animation (``change`` / ``direction``).
      - ``day_open`` — the session/day open price, held stable across ticks.
        Drives the watchlist's "daily change %" (``day_change_percent``).
    """

    ticker: str
    price: float
    previous_price: float
    day_open: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from the previous tick."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from the previous tick."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def day_change(self) -> float:
        """Absolute price change since the day/session open."""
        return round(self.price - self.day_open, 4)

    @property
    def day_change_percent(self) -> float:
        """Percentage change since the day/session open (the watchlist metric)."""
        if self.day_open == 0:
            return 0.0
        return round((self.price - self.day_open) / self.day_open * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat' relative to the previous tick."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "day_open": self.day_open,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "day_change": self.day_change,
            "day_change_percent": self.day_change_percent,
            "direction": self.direction,
        }
