"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    day_baseline_price: float | None = None
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def day_change(self) -> float:
        """Absolute same-business-day change from baseline."""
        baseline = self.day_baseline_price if self.day_baseline_price is not None else self.previous_price
        return round(self.price - baseline, 4)

    @property
    def day_change_percent(self) -> float:
        """Percentage same-business-day change from baseline."""
        baseline = self.day_baseline_price if self.day_baseline_price is not None else self.previous_price
        if baseline == 0:
            return 0.0
        return round((self.price - baseline) / baseline * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    @property
    def day_direction(self) -> str:
        """'up', 'down', or 'flat' relative to same-day baseline."""
        baseline = self.day_baseline_price if self.day_baseline_price is not None else self.previous_price
        if self.price > baseline:
            return "up"
        elif self.price < baseline:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "day_baseline_price": (
                self.day_baseline_price if self.day_baseline_price is not None else self.previous_price
            ),
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
            "day_change": self.day_change,
            "day_change_percent": self.day_change_percent,
            "day_direction": self.day_direction,
        }
