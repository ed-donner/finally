"""Shared test helpers for API/service/LLM tests."""

from __future__ import annotations

from app.market import PriceCache

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]


class FakeSource:
    """In-memory MarketDataSource stand-in (no background task, deterministic)."""

    def __init__(self, cache: PriceCache) -> None:
        self.cache = cache
        self.tickers: list[str] = []

    async def start(self, tickers: list[str]) -> None:
        self.tickers = list(tickers)

    async def stop(self) -> None:
        pass

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self.tickers:
            self.tickers.append(ticker)
        self.cache.update(ticker, 100.0)

    async def remove_ticker(self, ticker: str) -> None:
        if ticker in self.tickers:
            self.tickers.remove(ticker)
        self.cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self.tickers)


def seeded_cache(prices: dict[str, float] | None = None) -> PriceCache:
    """A PriceCache pre-populated with prices for the default tickers."""
    cache = PriceCache()
    for ticker in DEFAULT_TICKERS:
        cache.update(ticker, (prices or {}).get(ticker, 100.0))
    if prices:
        for ticker, price in prices.items():
            cache.update(ticker, price)
    return cache
