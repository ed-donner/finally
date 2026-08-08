from __future__ import annotations

import asyncio

from .types import PriceTick


class PriceCache:
    def __init__(self) -> None:
        self._ticks: dict[str, PriceTick] = {}
        self._lock = asyncio.Lock()

    async def update(self, tick: PriceTick) -> None:
        async with self._lock:
            self._ticks[tick.ticker] = tick

    async def get(self, ticker: str) -> PriceTick | None:
        async with self._lock:
            return self._ticks.get(ticker)

    async def snapshot(self) -> dict[str, PriceTick]:
        async with self._lock:
            return dict(self._ticks)  # shallow copy: consistent view, no tearing
