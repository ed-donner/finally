"""LLM test fixtures: temp DB + a stub MarketDataSource + a primed PriceCache."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import db
from app.market import MarketDataSource, PriceCache


class StubMarketSource(MarketDataSource):
    """In-memory stand-in for the real source: tracks add/remove calls."""

    def __init__(self) -> None:
        self.tickers: list[str] = []
        self.added: list[str] = []
        self.removed: list[str] = []
        self.started = False
        self.stopped = False

    async def start(self, tickers: list[str]) -> None:
        self.tickers = list(tickers)
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def add_ticker(self, ticker: str) -> None:
        self.added.append(ticker)
        if ticker not in self.tickers:
            self.tickers.append(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self.removed.append(ticker)
        if ticker in self.tickers:
            self.tickers.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self.tickers)


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    db_path = str(tmp_path / "llm.db")
    monkeypatch.setenv("FINALLY_DB_PATH", db_path)
    db.reset_initialization_state()
    db.ensure_initialized()
    yield db_path
    db.reset_initialization_state()
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
def primed_cache() -> PriceCache:
    cache = PriceCache()
    for ticker in db.DEFAULT_WATCHLIST:
        cache.update(ticker, 100.0)
    return cache


@pytest.fixture
def stub_source() -> StubMarketSource:
    return StubMarketSource()


@pytest.fixture
def mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MOCK", "true")
