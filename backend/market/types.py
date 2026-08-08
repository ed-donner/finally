from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Direction = Literal["up", "down", "flat"]


@dataclass(frozen=True)
class PriceTick:
    """A single ticker's latest price, as held in the cache and pushed over SSE."""
    ticker: str
    price: float
    previous_price: float
    direction: Direction
    timestamp: str  # ISO 8601 UTC, e.g. "2026-08-08T14:03:00.512000+00:00"


@dataclass(frozen=True)
class Bar:
    """One OHLC(V) candle for the historical detail chart."""
    t: int      # bar start, Unix milliseconds (matches Massive + frontend charts)
    o: float
    h: float
    low: float  # 'l' would shadow nothing but reads poorly; serialize back to "l"
    c: float
    v: float
