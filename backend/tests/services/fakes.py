"""Shared test doubles."""

from __future__ import annotations

from app.market import MarketDataSource, PriceCache, normalize_ticker

DEFAULT_TEST_PRICE = 50.0


class FakeSource(MarketDataSource):
    """Deterministic market source: seeds a fixed price for new tickers, never ticks."""

    def __init__(self, cache: PriceCache, prices: dict[str, float] | None = None) -> None:
        self.cache = cache
        self.prices = {normalize_ticker(k): v for k, v in (prices or {}).items()}
        self.tickers: set[str] = set()
        self.started = False
        self.stopped = False

    async def start(self, tickers: list[str]) -> None:
        self.started = True
        for t in tickers:
            await self.add_ticker(t)

    async def stop(self) -> None:
        self.stopped = True

    async def add_ticker(self, ticker: str) -> None:
        t = normalize_ticker(ticker)
        self.tickers.add(t)
        if self.cache.get(t) is None:
            self.cache.update(t, self.prices.get(t, DEFAULT_TEST_PRICE))

    async def remove_ticker(self, ticker: str) -> None:
        t = normalize_ticker(ticker)
        self.tickers.discard(t)
        self.cache.remove(t)

    def get_tickers(self) -> list[str]:
        return sorted(self.tickers)
