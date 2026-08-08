from __future__ import annotations

import time

from .gbm import SimEngine
from .source import MarketDataSource
from .types import Bar


class SimulatedSource(MarketDataSource):
    poll_interval_seconds = 0.5

    def __init__(self, seed: int | None = None) -> None:
        self._engine = SimEngine(seed)

    async def get_prices(self, tickers: list[str]) -> dict[str, float]:
        return self._engine.step(tickers)  # advance one tick, return prices

    async def get_history(self, ticker: str, days: int = 90) -> list[Bar]:
        end_ms = int(time.time() * 1000)
        return self._engine.history(ticker, days, end_ms)
