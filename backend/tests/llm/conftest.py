"""Fixtures for LLM tests: temp DB, seeded price cache, in-memory market source."""

from __future__ import annotations

import pytest

from app import db
from app.market import MarketDataSource, PriceCache, normalize_ticker

PRICES = {
    "AAPL": 190.0, "GOOGL": 175.0, "MSFT": 420.0, "AMZN": 185.0, "TSLA": 250.0,
    "NVDA": 880.0, "META": 500.0, "JPM": 200.0, "V": 280.0, "NFLX": 650.0, "PYPL": 65.0,
}


class FakeSource(MarketDataSource):
    """Tracks tickers in memory and seeds the cache with a fixed price on add."""

    def __init__(self, cache: PriceCache) -> None:
        self.cache = cache
        self.tickers: set[str] = set()

    async def start(self, tickers: list[str]) -> None:
        for t in tickers:
            await self.add_ticker(t)

    async def stop(self) -> None:
        pass

    async def add_ticker(self, ticker: str) -> None:
        t = normalize_ticker(ticker)
        self.tickers.add(t)
        if t not in self.cache:
            self.cache.update(t, PRICES.get(t, 100.0))

    async def remove_ticker(self, ticker: str) -> None:
        t = normalize_ticker(ticker)
        self.tickers.discard(t)
        self.cache.remove(t)

    def get_tickers(self) -> list[str]:
        return sorted(self.tickers)


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("FINALLY_DB_PATH", str(path))
    monkeypatch.delenv("LLM_MOCK", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    db.init_db(path)
    return path


@pytest.fixture
def price_cache() -> PriceCache:
    cache = PriceCache()
    for t, p in PRICES.items():
        cache.update(t, p)
    return cache


@pytest.fixture
async def source(price_cache) -> FakeSource:
    src = FakeSource(price_cache)
    await src.start(db.get_watchlist())
    return src


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setenv("LLM_MOCK", "true")
