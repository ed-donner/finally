# Market Data Backend — Complete Implementation Guide

**Purpose:** Unified market data subsystem for FinAlly supporting both a GBM simulator (default) and Massive API (real market data) via a single abstraction.

**Status:** Architecture complete. Production-ready code patterns provided for all components.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Data Model](#2-data-model)
3. [Unified Interface](#3-unified-interface)
4. [In-Memory Price Cache](#4-in-memory-price-cache)
5. [GBM Simulator Implementation](#5-gbm-simulator-implementation)
6. [Massive API Client Implementation](#6-massive-api-client-implementation)
7. [Factory Pattern](#7-factory-pattern)
8. [SSE Streaming Endpoint](#8-sse-streaming-endpoint)
9. [FastAPI Integration](#9-fastapi-integration)
10. [Testing Strategy](#10-testing-strategy)
11. [Error Handling & Edge Cases](#11-error-handling--edge-cases)
12. [Configuration Reference](#12-configuration-reference)

---

## 1. Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│ FastAPI Application                                          │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ PriceCache (Thread-Safe In-Memory Store)              │ │
│  │  - Single source of truth for all prices              │ │
│  │  - Version counter for efficient SSE updates          │ │
│  │  - Lock-protected for concurrent access               │ │
│  └────────────────────────────────────────────────────────┘ │
│    ▲                    ▲                    ▲              │
│    │                    │                    │              │
│  Writes              Writes              Reads             │
│    │                    │                    │              │
│  ┌─────────────┐  ┌────────────────┐  ┌──────────────┐    │
│  │ Simulator   │  │ Massive Client │  │ SSE Endpoint │    │
│  │ (GBM)       │  │ (REST Poller)  │  │ (Generator)  │    │
│  └─────────────┘  └────────────────┘  └──────────────┘    │
│                                              │              │
│                                              ▼              │
│                                        Connected           │
│                                        Clients             │
└──────────────────────────────────────────────────────────────┘
```

### Design Principles

- **Strategy Pattern**: Both data sources (simulator and Massive) implement the same abstract interface (`MarketDataSource`). Downstream code is source-agnostic.
- **Push-Based Updates**: Data sources write to the cache on their own schedule; consumers read when needed.
- **Thread Safety**: Shared cache uses `threading.Lock` to handle both async event loop and thread-pool access.
- **Immutable Values**: `PriceUpdate` objects are frozen dataclasses — safe to share without copying.
- **Lazy Imports**: The `massive` package only imports when actually needed (when `MASSIVE_API_KEY` is set).

---

## 2. Data Model

**File: `backend/app/market/models.py`**

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time.
    
    This is the only data structure that leaves the market data layer.
    All downstream consumers (SSE, portfolio, trades) work with this type.
    """

    ticker: str
    price: float
    previous_price: float
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
        pct = (self.price - self.previous_price) / self.previous_price * 100
        return round(pct, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat' based on price movement."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize to JSON for SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

### Key Features

- **`frozen=True`**: Immutable value objects. Safe to share across async tasks.
- **`slots=True`**: Minor memory optimization (many objects created per second).
- **Computed properties**: `change`, `direction`, `change_percent` derived from base fields — no risk of staleness.
- **`to_dict()`**: Single serialization point for both SSE and REST responses.

---

## 3. Unified Interface

**File: `backend/app/market/interface.py`**

```python
from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Abstract interface for market data providers.
    
    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code reads from the cache, not directly from the source.
    
    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        # ... app runs ...
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        # ... app shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.
        
        Starts a background task that periodically writes to the PriceCache.
        Must be called exactly once. Calling start() twice is undefined behavior.
        
        Args:
            tickers: List of ticker symbols to begin tracking
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.
        
        Safe to call multiple times. After stop(), the source will not write
        to the cache again.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present.
        
        The next update cycle will include this ticker's price.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. No-op if not present.
        
        Also removes the ticker from the PriceCache.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

### Why Push Instead of Pull?

This push model decouples timing:
- **Simulator**: Ticks at 500ms intervals
- **Massive**: Polls every 15s (free tier) or 2-5s (paid)
- **SSE**: Always reads from cache at its own 500ms cadence

There's no need for SSE to know or care about the underlying data source's schedule.

---

## 4. In-Memory Price Cache

**File: `backend/app/market/cache.py`**

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.
    
    Writers: SimulatorDataSource or MassiveDataSource (one at a time)
    Readers: SSE streaming endpoint, portfolio valuation, trade execution
    
    The version counter enables efficient SSE streaming — skip sends when
    prices haven't changed.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

    def update(
        self,
        ticker: str,
        price: float,
        timestamp: float | None = None,
    ) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        Automatically computes direction and change from the previous price.
        If this is the first update for the ticker, previous_price == price
        (direction='flat').
        
        Args:
            ticker: Ticker symbol
            price: Current price
            timestamp: Unix seconds (defaults to now)
            
        Returns:
            The created PriceUpdate
        """
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Get the latest price for a single ticker, or None if unknown.
        
        Args:
            ticker: Ticker symbol
            
        Returns:
            PriceUpdate or None
        """
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy.
        
        Returns:
            dict[ticker] -> PriceUpdate
        """
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: get just the price float, or None if unknown.
        
        Args:
            ticker: Ticker symbol
            
        Returns:
            Price as float or None
        """
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache (e.g., when removed from watchlist).
        
        Args:
            ticker: Ticker symbol
        """
        with self._lock:
            self._prices.pop(ticker, None)
            self._version += 1

    @property
    def version(self) -> int:
        """Current version counter. Used for SSE change detection.
        
        Returns:
            Version number (monotonically increasing)
        """
        return self._version

    def __len__(self) -> int:
        """Number of tickers in the cache."""
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        """Check if a ticker is in the cache."""
        with self._lock:
            return ticker in self._prices
```

### Version Counter Design

The version counter enables efficient SSE streaming. Without it, the SSE loop would serialize and send all prices every 500ms even if nothing changed (e.g., Massive API only updates every 15s). With versioning:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### Thread Safety Rationale

Uses `threading.Lock` instead of `asyncio.Lock` because:
- The Massive client's synchronous `get_snapshot_all()` runs in `asyncio.to_thread()` (real OS thread)
- `asyncio.Lock` would not protect against that
- `threading.Lock` works correctly from both sync threads and the async event loop
- Critical section is tiny (dict lookup + assignment) — no contention under normal load

---

## 5. GBM Simulator Implementation

### 5.1 Seed Prices & Parameters

**File: `backend/app/market/seed_prices.py`**

```python
"""Seed prices and per-ticker parameters for the market simulator.

Contains only constants — no logic, no imports beyond stdlib.
Shared by both simulator and potentially Massive client as fallback.
"""

# Realistic starting prices for the default watchlist
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 800.00,
    "META": 500.00,
    "JPM": 195.00,
    "V": 280.00,
    "NFLX": 600.00,
}

# Per-ticker GBM parameters
# sigma: annualized volatility (higher = more price movement)
# mu: annualized drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05},
    "AMZN": {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03},   # High volatility
    "NVDA": {"sigma": 0.40, "mu": 0.08},   # High volatility, strong drift
    "META": {"sigma": 0.30, "mu": 0.05},
    "JPM": {"sigma": 0.18, "mu": 0.04},    # Low volatility (bank)
    "V": {"sigma": 0.17, "mu": 0.04},      # Low volatility (payments)
    "NFLX": {"sigma": 0.35, "mu": 0.05},
}

# Default parameters for tickers not in TICKER_PARAMS
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for the simulator's Cholesky decomposition
# Tickers in the same group have higher intra-group correlation
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Correlation coefficients
INTRA_TECH_CORR = 0.6       # Tech stocks move together
INTRA_FINANCE_CORR = 0.5    # Finance stocks move together
CROSS_GROUP_CORR = 0.3      # Between sectors
TSLA_CORR = 0.3             # TSLA does its own thing
```

### 5.2 GBM Simulator Engine

**File: `backend/app/market/simulator.py`**

```python
from __future__ import annotations

import asyncio
import logging
import math
import random

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource
from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    TSLA_CORR,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices.

    Math:
        S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)

    Where:
        S(t)   = current price
        mu     = annualized drift (expected return)
        sigma  = annualized volatility
        dt     = time step as fraction of a trading year
        Z      = correlated standard normal random variable (from Cholesky)

    The tiny dt (~8.5e-8 for 500ms ticks over 252 trading days * 6.5h/day)
    produces sub-cent moves per tick that accumulate naturally over time.
    """

    # 500ms expressed as a fraction of a trading year
    # 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800 seconds
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ) -> None:
        """Initialize the simulator with a set of tickers.
        
        Args:
            tickers: List of ticker symbols to simulate
            dt: Time step as fraction of trading year (default: 500ms)
            event_probability: Chance of random event per ticker per tick (default: 0.001)
        """
        self._dt = dt
        self._event_prob = event_probability

        # Per-ticker state
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}

        # Cholesky decomposition of the correlation matrix (for correlated moves)
        self._cholesky: np.ndarray | None = None

        # Initialize all starting tickers
        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Returns {ticker: new_price}.

        This is the hot path — called every 500ms. Keep it fast.
        
        Returns:
            dict[ticker] -> price
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        # Generate n independent standard normal draws
        z_independent = np.random.standard_normal(n)

        # Apply Cholesky to get correlated draws
        if self._cholesky is not None:
            z_correlated = self._cholesky @ z_independent
        else:
            z_correlated = z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            # GBM: S(t+dt) = S(t) * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random event: ~0.1% chance per tick per ticker
            # With 10 tickers at 2 ticks/sec, expect an event ~every 50 seconds
            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign
                logger.debug(
                    "Random event on %s: %.1f%% %s",
                    ticker,
                    shock_magnitude * 100,
                    "up" if shock_sign > 0 else "down",
                )

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the simulation. Rebuilds the correlation matrix.
        
        Args:
            ticker: Ticker symbol
        """
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the simulation. Rebuilds the correlation matrix.
        
        Args:
            ticker: Ticker symbol
        """
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        """Current price for a ticker, or None if not tracked.
        
        Args:
            ticker: Ticker symbol
            
        Returns:
            Current price or None
        """
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        """Get the list of currently tracked tickers.
        
        Returns:
            List of ticker symbols
        """
        return list(self._tickers)

    # --- Internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add a ticker without rebuilding Cholesky (for batch initialization).
        
        Args:
            ticker: Ticker symbol
        """
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Rebuild the Cholesky decomposition of the ticker correlation matrix.

        Called whenever tickers are added or removed. O(n^2) but n < 50.
        """
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        # Build the correlation matrix
        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        """Determine correlation between two tickers based on sector grouping.

        Correlation structure:
          - Same tech sector:      0.6
          - Same finance sector:   0.5
          - TSLA with anything:    0.3 (it does its own thing)
          - Cross-sector:          0.3
          - Unknown tickers:       0.3
          
        Args:
            t1: First ticker
            t2: Second ticker
            
        Returns:
            Correlation coefficient
        """
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        # TSLA is in tech set but behaves independently
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR

        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR

        return CROSS_GROUP_CORR


class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator.

    Runs a background asyncio task that calls GBMSimulator.step() every
    `update_interval` seconds and writes results to the PriceCache.
    """

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
    ) -> None:
        """Initialize the simulator data source.
        
        Args:
            price_cache: Shared PriceCache to write to
            update_interval: Seconds between steps (default: 0.5)
            event_probability: Chance of random event per tick (default: 0.001)
        """
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        """Start the simulator with initial tickers.
        
        Seeds the cache with initial prices before starting the background loop
        so the SSE endpoint has data to send immediately.
        
        Args:
            tickers: Initial list of tickers to track
        """
        self._sim = GBMSimulator(
            tickers=tickers,
            event_probability=self._event_prob,
        )
        # Seed the cache with initial prices so SSE has data immediately
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(tickers))

    async def stop(self) -> None:
        """Stop the simulator and clean up."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Simulator stopped")

    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active simulation.
        
        Args:
            ticker: Ticker symbol
        """
        if self._sim:
            self._sim.add_ticker(ticker)
            # Seed cache immediately so the ticker has a price right away
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
            logger.info("Simulator: added ticker %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active simulation.
        
        Args:
            ticker: Ticker symbol
        """
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        """Get the list of currently tracked tickers.
        
        Returns:
            List of ticker symbols
        """
        return list(self._sim.get_tickers()) if self._sim else []

    async def _run_loop(self) -> None:
        """Core loop: step the simulation, write to cache, sleep."""
        while True:
            try:
                if self._sim:
                    prices = self._sim.step()
                    for ticker, price in prices.items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

### GBM Simulator Highlights

- **Immediate seeding**: When `start()` is called, the cache is populated with seed prices *before* the loop begins. SSE endpoint has data on first tick (no blank screen).
- **Graceful cancellation**: `stop()` properly awaits the task and catches `CancelledError`.
- **Exception resilience**: The loop catches exceptions per-step so one bad tick doesn't kill the feed.
- **Correlated moves**: Cholesky decomposition ensures tech stocks, finance stocks, etc. move realistically together.

---

## 6. Massive API Client Implementation

**File: `backend/app/market/massive_client.py`**

```python
from __future__ import annotations

import asyncio
import logging

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call, then writes results to the PriceCache.

    Rate limits:
      - Free tier: 5 req/min → poll every 15s (default)
      - Paid tiers: higher limits → poll every 2-5s

    The synchronous Massive client runs in asyncio.to_thread() to avoid
    blocking the event loop.
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        """Initialize the Massive data source.
        
        Args:
            api_key: Massive API key
            price_cache: Shared PriceCache to write to
            poll_interval: Seconds between polls (default: 15.0 for free tier)
        """
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: object | None = None  # Lazy import

    async def start(self, tickers: list[str]) -> None:
        """Start polling the Massive API for initial tickers.
        
        Performs an immediate first poll so the cache has data right away.
        
        Args:
            tickers: Initial list of tickers to track
        """
        # Lazy import: only import massive when actually using real market data.
        # This means the massive package is not required when using the simulator.
        from massive import RESTClient

        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)

        # Do an immediate first poll so the cache has data right away
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval",
            len(tickers),
            self._interval,
        )

    async def stop(self) -> None:
        """Stop polling and clean up."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set.
        
        Args:
            ticker: Ticker symbol
        """
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set.
        
        Args:
            ticker: Ticker symbol
        """
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        """Get the list of currently tracked tickers.
        
        Returns:
            List of ticker symbols
        """
        return list(self._tickers)

    # --- Internal ---

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Execute one poll cycle: fetch snapshots, update cache."""
        if not self._tickers or not self._client:
            return

        try:
            # The Massive RESTClient is synchronous — run in a thread to
            # avoid blocking the event loop.
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    # Massive timestamps are Unix milliseconds → convert to seconds
                    timestamp = snap.last_trade.timestamp / 1000.0
                    self._cache.update(
                        ticker=snap.ticker,
                        price=price,
                        timestamp=timestamp,
                    )
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s",
                        getattr(snap, "ticker", "???"),
                        e,
                    )
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — the loop will retry on the next interval.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread pool.
        
        Returns:
            List of snapshot objects
        """
        from massive.rest.models import SnapshotMarketType

        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Massive Client Highlights

- **Lazy imports**: `from massive import ...` happens inside `start()`, not at module import time. The `massive` package is only needed when `MASSIVE_API_KEY` is set.
- **Thread-safe async**: The synchronous Massive client runs in `asyncio.to_thread()` to avoid blocking the event loop.
- **Resilient error handling**: All exceptions are caught and logged. The poller keeps running even if a poll fails (user might fix the API key and restart).
- **Individual ticker error handling**: If one snapshot is malformed, it's skipped with a warning. Other tickers still processed.

---

## 7. Factory Pattern

**File: `backend/app/market/factory.py`**

```python
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment variables.

    Selection logic:
      - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
      - Otherwise → SimulatorDataSource (GBM simulation)

    Returns an unstarted source. Caller must await source.start(tickers).
    
    Args:
        price_cache: Shared PriceCache
        
    Returns:
        A MarketDataSource implementation
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource

        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        from .simulator import SimulatorDataSource

        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

### Usage at App Startup

```python
from app.market import PriceCache, create_market_data_source

# Create cache and source
price_cache = PriceCache()
source = create_market_data_source(price_cache)

# Start with initial tickers from database
initial_tickers = ["AAPL", "GOOGL", "MSFT", ...]
await source.start(initial_tickers)

# Store in app state for later access
app.state.price_cache = price_cache
app.state.market_source = source
```

---

## 8. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

```python
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create the SSE streaming router with a reference to the price cache.

    This factory pattern lets us inject the PriceCache without globals.
    
    Args:
        price_cache: Shared PriceCache to read from
        
    Returns:
        FastAPI APIRouter with /prices SSE endpoint
    """

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint for live price updates.

        Streams all tracked ticker prices every ~500ms. The client connects
        with EventSource and receives events in the format:

            data: {"AAPL": {"ticker": "AAPL", "price": 190.50, ...}, ...}

        Includes a retry directive so the browser auto-reconnects on
        disconnection (EventSource has built-in support).
        
        Args:
            request: FastAPI request object (used to detect disconnect)
            
        Returns:
            StreamingResponse with text/event-stream media type
        """
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering if proxied
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> None:
    """Async generator that yields SSE-formatted price events.

    Sends all prices every `interval` seconds. Stops when the client
    disconnects (detected via request.is_disconnected()).
    
    Args:
        price_cache: Shared PriceCache to read from
        request: FastAPI request object
        interval: Seconds between sends (default: 0.5)
        
    Yields:
        SSE-formatted strings
    """
    # Tell the client to retry after 1 second if the connection drops
    yield "retry: 1000\n\n"

    last_version = -1
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            # Check for client disconnect
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            # Only send if cache has been updated
            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()

                if prices:
                    data = {
                        ticker: update.to_dict()
                        for ticker, update in prices.items()
                    }
                    payload = json.dumps(data)
                    yield f"data: {payload}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### SSE Wire Format

Each event the client receives looks like:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{"ticker":"GOOGL",...}}

```

Client-side parsing:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices is { "AAPL": { ticker, price, previous_price, ... }, ... }
    updateUI(prices);
};
```

### Why Version-Based Polling?

Instead of an event-driven model, the SSE endpoint polls the cache on a fixed interval. This is simpler and produces predictable, evenly-spaced updates for the frontend. The version counter avoids sending redundant payloads when the Massive API hasn't polled yet (free tier: every 15s).

---

## 9. FastAPI Integration

**File: `backend/app/main.py` (excerpt)**

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends

from app.market import (
    PriceCache,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
)
from app.db import load_watchlist_tickers  # Your database module


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of background services."""

    # --- STARTUP ---

    # 1. Create the shared price cache
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # 2. Create and start the market data source
    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # 3. Load initial tickers from the database watchlist
    initial_tickers = await load_watchlist_tickers()
    await source.start(initial_tickers)

    # 4. Register the SSE streaming router
    stream_router = create_stream_router(price_cache)
    app.include_router(stream_router)

    yield  # App is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(
    title="FinAlly",
    description="AI Trading Workstation",
    lifespan=lifespan,
)


# Dependency injection for route handlers
def get_price_cache() -> PriceCache:
    """Inject the PriceCache into route handlers."""
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    """Inject the MarketDataSource into route handlers."""
    return app.state.market_source


# Example: Using the cache in a route
@app.post("/api/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    """Execute a trade at current market price."""
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(
            status_code=400,
            detail=f"No price available for {trade.ticker}. Please wait and try again.",
        )
    # ... execute trade at current_price ...


# Example: Managing watchlist changes
@app.post("/api/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
    price_cache: PriceCache = Depends(get_price_cache),
):
    """Add a ticker to the watchlist and start tracking it."""
    # Add to database
    await db.insert_watchlist_entry(payload.ticker)
    
    # Tell the data source to start tracking
    await source.add_ticker(payload.ticker)
    
    # Return current price if available
    price = price_cache.get_price(payload.ticker)
    return {"ticker": payload.ticker, "price": price}


@app.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
    price_cache: PriceCache = Depends(get_price_cache),
):
    """Remove a ticker from the watchlist."""
    # Remove from database
    await db.delete_watchlist_entry(ticker)
    
    # Check if ticker has an open position
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        # Only stop tracking if no open position
        await source.remove_ticker(ticker)
    
    return {"status": "ok"}
```

### Key Patterns

- **Lifespan context manager**: Manages startup/shutdown of background services within FastAPI's lifecycle.
- **Dependency injection**: `Depends()` injects `PriceCache` and `MarketDataSource` into route handlers without globals.
- **Watchlist coordination**: When watchlist changes, inform the data source so it tracks the right tickers.
- **Open position safeguard**: Don't stop tracking a ticker if the user still holds shares.

---

## 10. Testing Strategy

### Unit Test: GBMSimulator

```python
# backend/tests/market/test_simulator.py
import pytest
from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES


class TestGBMSimulator:
    """Unit tests for the GBM price simulator."""

    def test_step_returns_all_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        result = sim.step()
        assert set(result.keys()) == {"AAPL", "GOOGL"}

    def test_prices_are_positive(self):
        """GBM prices can never go negative (exp() is always positive)."""
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(10_000):
            prices = sim.step()
            assert prices["AAPL"] > 0

    def test_initial_prices_match_seeds(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

    def test_add_ticker(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("TSLA")
        result = sim.step()
        assert "TSLA" in result

    def test_remove_ticker(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        sim.remove_ticker("GOOGL")
        result = sim.step()
        assert "GOOGL" not in result
        assert "AAPL" in result
```

### Unit Test: PriceCache

```python
# backend/tests/market/test_cache.py
import pytest
from app.market.cache import PriceCache


class TestPriceCache:

    def test_update_and_get(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.ticker == "AAPL"
        assert update.price == 190.50
        assert cache.get("AAPL") == update

    def test_direction_up(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 191.00)
        assert update.direction == "up"
        assert update.change == 1.00

    def test_direction_down(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 189.00)
        assert update.direction == "down"
        assert update.change == -1.00

    def test_version_increments(self):
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        assert cache.version == v0 + 1
```

### Integration Test: SimulatorDataSource

```python
# backend/tests/market/test_simulator_source.py
import asyncio
import pytest
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
class TestSimulatorDataSource:

    async def test_start_populates_cache(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])

        # Cache should have seed prices immediately
        assert cache.get("AAPL") is not None
        assert cache.get("GOOGL") is not None

        await source.stop()

    async def test_add_and_remove_ticker(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])

        await source.add_ticker("TSLA")
        assert "TSLA" in source.get_tickers()
        assert cache.get("TSLA") is not None

        await source.remove_ticker("TSLA")
        assert "TSLA" not in source.get_tickers()
        assert cache.get("TSLA") is None

        await source.stop()
```

---

## 11. Error Handling & Edge Cases

### Empty Watchlist at Startup

If the database has no watchlist entries, `start()` receives an empty list. Both data sources handle gracefully:

```python
# Simulator: no prices generated
if n == 0:
    return {}

# Massive: skips API call
if not self._tickers or not self._client:
    return
```

SSE sends empty events. When the user adds a ticker, the source starts tracking immediately.

### Price Cache Miss During Trade

If a user tries to trade a ticker with no cached price (e.g., just added, Massive hasn't polled):

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait and try again.",
    )
```

The simulator avoids this by seeding the cache in `add_ticker()`. Massive may have brief gaps — HTTP 400 with clear messaging is correct.

### Massive API Key Invalid

If the key is invalid, the first poll fails with 401. The poller logs and keeps retrying. SSE streams empty data. User sees no prices and a connection status indicator. Fix: correct the key and restart.

### Simulator Precision

GBM with tiny `dt` produces small per-tick moves. Floating-point precision is not a concern:
- Prices rounded to 2 decimal places
- Exponential formulation is numerically stable
- Prices always positive (exponential function)

---

## 12. Configuration Reference

All tunable parameters and their defaults:

| Parameter | Location | Default | Description |
|-----------|----------|---------|-------------|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set, use Massive; otherwise use simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` seconds | Time between simulator ticks |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` seconds | Time between Massive polls (free tier) |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of random shock per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `~8.5e-8` | GBM time step (fraction of trading year) |
| SSE push interval | `_generate_events()` | `0.5` seconds | Time between SSE pushes to client |
| SSE retry directive | `_generate_events()` | `1000` ms | Browser EventSource reconnection delay |

### Package `__init__.py`

**File: `backend/app/market/__init__.py`**

```python
"""Market data subsystem for FinAlly.

Public API:
    PriceUpdate              - Immutable price snapshot dataclass
    PriceCache               - Thread-safe in-memory price store
    MarketDataSource         - Abstract interface for data providers
    create_market_data_source - Factory that selects simulator or Massive
    create_stream_router     - FastAPI router factory for SSE endpoint
"""

from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
]
```

---

## Implementation Checklist

- [ ] Create `backend/app/market/models.py` with `PriceUpdate` dataclass
- [ ] Create `backend/app/market/cache.py` with `PriceCache` class
- [ ] Create `backend/app/market/interface.py` with `MarketDataSource` ABC
- [ ] Create `backend/app/market/seed_prices.py` with constants
- [ ] Create `backend/app/market/simulator.py` with `GBMSimulator` and `SimulatorDataSource`
- [ ] Create `backend/app/market/massive_client.py` with `MassiveDataSource`
- [ ] Create `backend/app/market/factory.py` with `create_market_data_source()`
- [ ] Create `backend/app/market/stream.py` with SSE endpoint
- [ ] Create `backend/app/market/__init__.py` with public API
- [ ] Integrate with FastAPI lifespan in `backend/app/main.py`
- [ ] Write unit tests for each module
- [ ] Write integration tests for data sources
- [ ] Test SSE endpoint with mock client
- [ ] Verify Cholesky decomposition works for all 10 default tickers
- [ ] Test error cases (invalid API key, malformed responses, etc.)
- [ ] Add `numpy` and `massive` to `backend/pyproject.toml`
- [ ] Document environment variables in `.env.example`

---

## Quick Start Example

```python
# backend/app/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.market import (
    PriceCache,
    create_market_data_source,
    create_stream_router,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create and start market data
    cache = PriceCache()
    source = create_market_data_source(cache)
    await source.start(["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", 
                        "NVDA", "META", "JPM", "V", "NFLX"])
    
    app.state.price_cache = cache
    app.state.market_source = source
    
    # Register SSE endpoint
    app.include_router(create_stream_router(cache))
    
    yield
    
    # Shutdown
    await source.stop()

app = FastAPI(lifespan=lifespan)
```

**Run the app:**

```bash
# Using simulator (default)
cd backend
uv sync
uv run uvicorn app.main:app --reload

# Using Massive API
cd backend
MASSIVE_API_KEY=your-key-here uv run uvicorn app.main:app --reload
```

**Verify SSE is working:**

```bash
curl -N http://localhost:8000/api/stream/prices
```

Should see continuous JSON payloads with price updates.

