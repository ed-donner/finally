"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    prev_price: float       # Price from the previous update
    open_price: float       # Session-start seed price — set once, never overwritten
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from previous update."""
        return round(self.price - self.prev_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update."""
        if self.prev_price == 0:
            return 0.0
        return round((self.price - self.prev_price) / self.prev_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.prev_price:
            return "up"
        elif self.price < self.prev_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission.

        timestamp is formatted as ISO 8601 UTC string per PLAN.md §6.
        """
        ts_iso = datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        return {
            "ticker": self.ticker,
            "price": self.price,
            "prev_price": self.prev_price,
            "open_price": self.open_price,
            "timestamp": ts_iso,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
