from dataclasses import dataclass
from datetime import datetime


@dataclass
class PriceUpdate:
    ticker: str
    price: float
    prev_price: float
    timestamp: datetime
    change: float       # absolute dollar change
    change_pct: float   # percentage change (e.g. 1.23 = 1.23%)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat' — used by frontend flash animation."""
        if self.change > 0:
            return "up"
        elif self.change < 0:
            return "down"
        return "flat"


@dataclass
class DailyBar:
    ticker: str
    date: str           # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None = None
