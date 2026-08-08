from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from .cache import PriceCache
from .source import MarketDataSource
from .types import Direction, PriceTick

log = logging.getLogger("finally.market.feed")


def _direction(new: float, prev: float) -> Direction:
    if new > prev:
        return "up"
    if new < prev:
        return "down"
    return "flat"


class MarketFeed:
    def __init__(
        self,
        source: MarketDataSource,
        cache: PriceCache,
        get_active_tickers: Callable[[], list[str]],
    ) -> None:
        self._source = source
        self._cache = cache
        self._get_active_tickers = get_active_tickers
        self._task: asyncio.Task | None = None

    async def prime(self) -> None:
        """Populate the cache once before serving so the first SSE/portfolio
        request has data even on a slow (15s) Massive interval."""
        await self._tick_once()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._source.aclose()

    async def _tick_once(self) -> None:
        # get_active_tickers hits SQLite synchronously; run off the event
        # loop thread so a slow disk read never stalls connected SSE clients.
        tickers = await asyncio.to_thread(self._get_active_tickers)
        if not tickers:
            return
        try:
            prices = await self._source.get_prices(tickers)
        except Exception:  # source is defensive already; belt-and-suspenders
            log.exception("source.get_prices raised; keeping cached values")
            return
        now = datetime.now(timezone.utc).isoformat()
        for ticker, price in prices.items():
            prev = await self._cache.get(ticker)
            previous = prev.price if prev else price
            await self._cache.update(PriceTick(
                ticker=ticker,
                price=price,
                previous_price=previous,
                direction=_direction(price, previous),
                timestamp=now,
            ))

    async def _run(self) -> None:
        while True:
            await self._tick_once()
            await asyncio.sleep(self._source.poll_interval_seconds)
