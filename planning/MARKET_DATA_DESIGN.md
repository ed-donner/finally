# Market Data Backend — Implementation Design

This document is the implementation blueprint for the market data subsystem. It consolidates the interface contract ([MARKET_INTERFACE.md](./MARKET_INTERFACE.md)), simulator math ([MARKET_SIMULATOR.md](./MARKET_SIMULATOR.md)), and Massive API reference ([MASSIVE_API.md](./MASSIVE_API.md)) into a single, code-complete guide that a developer (or agent) can follow file-by-file.

---

## 1. File Structure

```
backend/
  src/
    market/
      __init__.py              # Package exports
      models.py                # PriceUpdate dataclass
      cache.py                 # PriceCache (thread-safe in-memory store)
      interface.py             # MarketDataSource ABC
      factory.py               # create_market_data_source()
      seed_prices.py           # SEED_PRICES, TICKER_PROFILES, DEFAULT_SEED_RANGE
      engine.py                # SimulationEngine (GBM math, correlation, events)
      simulator.py             # SimulatorMarketData (MarketDataSource impl)
      massive_client.py        # MassiveMarketData (MarketDataSource impl)
```

Every file below is presented in full. Copy each one verbatim into the path shown.

---

## 2. `models.py` — Core Data Model

```python
"""Universal price update model used by all market data sources and the SSE stream."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """A single price update for one ticker.

    Attributes:
        ticker:     Uppercase stock symbol (e.g. "AAPL").
        price:      Current price in USD.
        prev_price: Previous price (for computing flash direction).
        timestamp:  UTC timestamp of this update.
        direction:  One of "up", "down", or "unchanged".
    """

    ticker: str
    price: float
    prev_price: float
    timestamp: datetime
    direction: str  # "up" | "down" | "unchanged"

    def to_sse_dict(self) -> dict:
        """Serialize to the JSON shape sent over the SSE stream."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "prev_price": self.prev_price,
            "timestamp": self.timestamp.isoformat(),
            "direction": self.direction,
        }
```

### Design notes

- `frozen=True` makes instances immutable and hashable — safe to pass between async tasks without copying.
- `slots=True` reduces memory overhead (we create thousands of these over a session).
- `to_sse_dict()` centralizes the serialization format so the SSE endpoint doesn't reimplement it.

---

## 3. `cache.py` — Thread-Safe Price Cache

```python
"""In-memory cache of the latest PriceUpdate per ticker.

Both SimulatorMarketData and MassiveMarketData write to a PriceCache instance.
The SSE endpoint and REST APIs read from it. Access is serialized with a
threading.Lock because the simulator's background task may run on a thread-pool
executor in some uvicorn configurations.
"""

from __future__ import annotations

import threading
from datetime import datetime

from .models import PriceUpdate


class PriceCache:
    """Thread-safe store of the most recent price per ticker."""

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = threading.Lock()

    # ── writes ──────────────────────────────────────────────

    def update(self, ticker: str, price: float, timestamp: datetime) -> PriceUpdate:
        """Record a new price. Computes direction from the previous value.

        Returns the newly created PriceUpdate so callers can use it
        immediately without a second lookup.
        """
        with self._lock:
            prev = self._prices.get(ticker)
            prev_price = prev.price if prev else price

            if price > prev_price:
                direction = "up"
            elif price < prev_price:
                direction = "down"
            else:
                direction = "unchanged"

            update = PriceUpdate(
                ticker=ticker,
                price=price,
                prev_price=prev_price,
                timestamp=timestamp,
                direction=direction,
            )
            self._prices[ticker] = update
            return update

    def remove(self, ticker: str) -> None:
        """Stop tracking a ticker (called on watchlist removal)."""
        with self._lock:
            self._prices.pop(ticker, None)

    # ── reads ───────────────────────────────────────────────

    def get(self, ticker: str) -> PriceUpdate | None:
        """Return the latest update for one ticker, or None."""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Return a snapshot of all tracked tickers. The returned dict is a
        shallow copy so callers can iterate without holding the lock."""
        with self._lock:
            return dict(self._prices)

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Why a lock instead of asyncio primitives?

The SSE endpoint runs in an async context, but the cache may also be read from synchronous helper functions (e.g., portfolio valuation during trade execution). A `threading.Lock` works in both contexts and adds negligible overhead at this contention level.

---

## 4. `interface.py` — Abstract Base Class

```python
"""Abstract interface that all market data sources implement.

Downstream code — SSE streaming, portfolio valuation, trade execution —
depends only on this interface. The concrete implementation is selected at
startup by the factory function in factory.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import PriceUpdate


class MarketDataSource(ABC):
    """Contract for market data providers."""

    @abstractmethod
    async def start(self) -> None:
        """Start the background task that produces price updates.

        For the simulator this launches the GBM tick loop.
        For Massive this launches the REST polling loop.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully cancel the background task and release resources."""

    @abstractmethod
    def register_ticker(self, ticker: str, seed_price: float | None = None) -> None:
        """Begin tracking a ticker.

        Args:
            ticker:     Uppercase stock symbol.
            seed_price: Starting price hint. Used by the simulator; ignored
                        by the Massive client (real price comes from the API).
        """

    @abstractmethod
    def unregister_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker and remove it from the price cache."""

    @abstractmethod
    def get_latest(self, ticker: str) -> PriceUpdate | None:
        """Return the most recent price update for a ticker, or None."""

    @abstractmethod
    def get_all_latest(self) -> dict[str, PriceUpdate]:
        """Return the most recent price update for every tracked ticker."""
```

---

## 5. `seed_prices.py` — Ticker Configuration

```python
"""Seed prices and per-ticker simulation parameters.

Used by the simulator to initialize realistic starting prices and GBM
parameters. The Massive client ignores seed prices (real prices come from
the API) but uses SEED_PRICES as a fallback lookup for the cache if
needed before the first poll completes.
"""

from __future__ import annotations

# Realistic starting prices for the default watchlist (approx. early 2025)
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 130.00,
    "META": 500.00,
    "JPM": 195.00,
    "V": 280.00,
    "NFLX": 650.00,
}

# When a ticker isn't in SEED_PRICES, the simulator picks a random price
# uniformly from this range.
DEFAULT_SEED_RANGE: tuple[float, float] = (50.0, 300.0)

# Per-ticker GBM parameters and sector grouping for correlation.
#   mu    – annualized drift (expected return), e.g. 0.10 = 10%/year
#   sigma – annualized volatility,             e.g. 0.25 = 25%/year
#   sector – correlation group (same-sector tickers move together)
TICKER_PROFILES: dict[str, dict] = {
    "AAPL":  {"mu": 0.10, "sigma": 0.22, "sector": "tech"},
    "GOOGL": {"mu": 0.08, "sigma": 0.25, "sector": "tech"},
    "MSFT":  {"mu": 0.10, "sigma": 0.20, "sector": "tech"},
    "AMZN":  {"mu": 0.12, "sigma": 0.28, "sector": "tech"},
    "TSLA":  {"mu": 0.15, "sigma": 0.50, "sector": "auto"},
    "NVDA":  {"mu": 0.15, "sigma": 0.40, "sector": "tech"},
    "META":  {"mu": 0.10, "sigma": 0.30, "sector": "tech"},
    "JPM":   {"mu": 0.06, "sigma": 0.18, "sector": "finance"},
    "V":     {"mu": 0.08, "sigma": 0.16, "sector": "finance"},
    "NFLX":  {"mu": 0.12, "sigma": 0.35, "sector": "media"},
}

# Defaults for tickers not in TICKER_PROFILES
DEFAULT_MU = 0.08
DEFAULT_SIGMA = 0.25
DEFAULT_SECTOR = "other"
```

---

## 6. `engine.py` — GBM Simulation Engine

```python
"""Geometric Brownian Motion engine with correlated sector moves and random events.

This module contains pure math — no async, no I/O. It is called once per tick
by SimulatorMarketData to advance all tracked prices by one time step.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field

from .seed_prices import (
    DEFAULT_MU,
    DEFAULT_SECTOR,
    DEFAULT_SEED_RANGE,
    DEFAULT_SIGMA,
    SEED_PRICES,
    TICKER_PROFILES,
)

# Tickers in the same sector share this fraction of their random shock.
# 0.6 means 60% of the move is shared, 40% is idiosyncratic.
INTRA_SECTOR_CORRELATION: float = 0.6

# Per-tick probability of a random "event" (sudden 2-5% move on one ticker).
EVENT_PROBABILITY: float = 0.02

# Range of event magnitude (as a fraction of price).
EVENT_MIN_MAGNITUDE: float = 0.02
EVENT_MAX_MAGNITUDE: float = 0.05

# Minimum allowed price after a tick (prevents zero/negative).
MIN_PRICE: float = 0.01


@dataclass
class TickerState:
    """Mutable state for one ticker in the simulation."""

    price: float
    mu: float
    sigma: float
    sector: str


class SimulationEngine:
    """Generates correlated GBM price updates for a set of tickers.

    Usage:
        engine = SimulationEngine(tick_interval=0.5)
        engine.add_ticker("AAPL")
        engine.add_ticker("GOOGL")
        prices = engine.tick()  # {"AAPL": 190.12, "GOOGL": 174.88}
    """

    def __init__(self, tick_interval: float = 0.5) -> None:
        self._tickers: dict[str, TickerState] = {}
        # Convert tick interval to a fraction of a trading year.
        # 252 trading days × 6.5 hours/day × 3600 seconds/hour
        self._dt: float = tick_interval / (252 * 6.5 * 3600)
        self._rng = random.Random()

    # ── ticker management ───────────────────────────────────

    def add_ticker(self, ticker: str, seed_price: float | None = None) -> None:
        """Register a ticker with the engine.

        If the ticker is already tracked, this is a no-op.

        Args:
            ticker:     Uppercase symbol.
            seed_price: Explicit starting price. Falls back to SEED_PRICES
                        lookup, then to a random value in DEFAULT_SEED_RANGE.
        """
        ticker = ticker.upper()
        if ticker in self._tickers:
            return

        profile = TICKER_PROFILES.get(ticker, {})
        price = seed_price or SEED_PRICES.get(ticker)
        if price is None:
            price = self._rng.uniform(*DEFAULT_SEED_RANGE)

        self._tickers[ticker] = TickerState(
            price=price,
            mu=profile.get("mu", DEFAULT_MU),
            sigma=profile.get("sigma", DEFAULT_SIGMA),
            sector=profile.get("sector", DEFAULT_SECTOR),
        )

    def remove_ticker(self, ticker: str) -> None:
        """Unregister a ticker. Future ticks will not include it."""
        self._tickers.pop(ticker.upper(), None)

    @property
    def tracked_tickers(self) -> list[str]:
        """Return a sorted list of currently tracked tickers."""
        return sorted(self._tickers.keys())

    # ── price generation ────────────────────────────────────

    def tick(self) -> dict[str, float]:
        """Advance every tracked ticker by one time step.

        Returns a dict mapping ticker → new price (rounded to 2 decimals).

        The algorithm:
        1. Group tickers by sector.
        2. Draw one shared N(0,1) shock per sector.
        3. For each ticker, draw an independent N(0,1) shock and blend it
           with the sector shock using the correlation coefficient.
        4. Apply the GBM formula:
              S(t+dt) = S(t) * exp((mu - sigma²/2)*dt + sigma*sqrt(dt)*Z)
        5. With 2% probability, apply a random event (sudden 2-5% move)
           to one randomly chosen ticker.
        6. Clamp prices to MIN_PRICE.
        """
        if not self._tickers:
            return {}

        # Step 1: group by sector
        sectors: dict[str, list[str]] = defaultdict(list)
        for ticker, state in self._tickers.items():
            sectors[state.sector].append(ticker)

        # Step 2: sector-level shocks
        sector_shocks: dict[str, float] = {
            sector: self._rng.gauss(0, 1) for sector in sectors
        }

        # Step 5 (early): decide if an event fires this tick
        event_ticker: str | None = None
        event_shock: float = 0.0
        if self._rng.random() < EVENT_PROBABILITY and self._tickers:
            event_ticker = self._rng.choice(list(self._tickers.keys()))
            magnitude = self._rng.uniform(EVENT_MIN_MAGNITUDE, EVENT_MAX_MAGNITUDE)
            event_shock = magnitude if self._rng.random() > 0.5 else -magnitude

        rho = INTRA_SECTOR_CORRELATION
        sqrt_one_minus_rho2 = math.sqrt(1 - rho ** 2)

        results: dict[str, float] = {}

        for ticker, state in self._tickers.items():
            # Step 3: blend sector + idiosyncratic shocks
            z_sector = sector_shocks[state.sector]
            z_indiv = self._rng.gauss(0, 1)
            z = rho * z_sector + sqrt_one_minus_rho2 * z_indiv

            # Step 4: GBM
            drift = (state.mu - 0.5 * state.sigma ** 2) * self._dt
            diffusion = state.sigma * math.sqrt(self._dt) * z
            new_price = state.price * math.exp(drift + diffusion)

            # Step 5: apply event if selected
            if ticker == event_ticker:
                new_price *= 1 + event_shock

            # Step 6: clamp
            new_price = max(new_price, MIN_PRICE)

            # Store and return
            state.price = new_price
            results[ticker] = round(new_price, 2)

        return results
```

### Key math explanation

The time step `dt` converts the 500ms tick interval into a fraction of a trading year:

```
dt = 0.5 / (252 × 6.5 × 3600) ≈ 8.48 × 10⁻⁸
```

This keeps per-tick volatility small. Over ~46,800 ticks (one simulated trading day), cumulative drift and volatility approximate realistic daily price ranges. For example, with sigma = 0.25 (AAPL-like volatility), the daily standard deviation is roughly:

```
daily_sigma ≈ 0.25 / sqrt(252) ≈ 1.58%
```

For AAPL at $190, that's about ±$3, which matches real market behavior.

### Correlation mechanics

The blending formula:

```
Z_ticker = ρ × Z_sector + √(1 - ρ²) × Z_individual
```

Produces a variable with unit variance (important for GBM correctness) where the correlation between any two same-sector tickers is exactly ρ = 0.6. Cross-sector tickers are uncorrelated. This makes the watchlist feel realistic — when AAPL dips, MSFT and NVDA tend to dip too.

---

## 7. `simulator.py` — Simulator Implementation

```python
"""Market data source that generates simulated prices using GBM.

This is the default data source when no MASSIVE_API_KEY is configured.
It runs an async background loop that calls the SimulationEngine every
500ms and writes results to the shared PriceCache.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .cache import PriceCache
from .engine import SimulationEngine
from .interface import MarketDataSource
from .models import PriceUpdate


class SimulatorMarketData(MarketDataSource):
    """In-process market data simulator. No external dependencies."""

    def __init__(self, tick_interval: float = 0.5) -> None:
        self._tick_interval = tick_interval
        self._cache = PriceCache()
        self._engine = SimulationEngine(tick_interval=tick_interval)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Launch the tick loop as a background asyncio task."""
        self._task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        """Cancel the tick loop and wait for clean shutdown."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def register_ticker(self, ticker: str, seed_price: float | None = None) -> None:
        """Add a ticker. It will appear in the next tick cycle."""
        self._engine.add_ticker(ticker.upper(), seed_price)

    def unregister_ticker(self, ticker: str) -> None:
        """Remove a ticker from both the engine and the cache."""
        ticker = ticker.upper()
        self._engine.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_latest(self, ticker: str) -> PriceUpdate | None:
        return self._cache.get(ticker.upper())

    def get_all_latest(self) -> dict[str, PriceUpdate]:
        return self._cache.get_all()

    async def _tick_loop(self) -> None:
        """Run forever, advancing prices every tick_interval seconds."""
        while True:
            prices = self._engine.tick()
            now = datetime.now(timezone.utc)
            for ticker, price in prices.items():
                self._cache.update(ticker, price, now)
            await asyncio.sleep(self._tick_interval)
```

---

## 8. `massive_client.py` — Massive (Polygon.io) Implementation

```python
"""Market data source backed by the Massive (Polygon.io) REST API.

Activated when the MASSIVE_API_KEY environment variable is set. Polls the
snapshot endpoint at a configurable interval (default 15s for free tier).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from .cache import PriceCache
from .interface import MarketDataSource
from .models import PriceUpdate

logger = logging.getLogger(__name__)

# Massive API (Polygon.io rebrand) base URL
BASE_URL = "https://api.polygon.io"

# Snapshot endpoint — returns current price data for specified tickers
SNAPSHOT_PATH = "/v2/snapshot/locale/us/markets/stocks/tickers"


class MassiveMarketData(MarketDataSource):
    """REST-polling market data source using the Massive snapshot API.

    One API call per poll returns data for all watched tickers. The free
    tier allows 5 requests/minute, so the default poll interval is 15s.
    """

    def __init__(
        self,
        api_key: str,
        poll_interval: float = 15.0,
        request_timeout: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._poll_interval = poll_interval
        self._request_timeout = request_timeout
        self._tickers: set[str] = set()
        self._cache = PriceCache()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Launch the polling loop as a background asyncio task."""
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Cancel the polling loop and wait for clean shutdown."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def register_ticker(self, ticker: str, seed_price: float | None = None) -> None:
        """Add a ticker to the poll set. seed_price is ignored — real price
        comes from the API on the next poll cycle."""
        self._tickers.add(ticker.upper())

    def unregister_ticker(self, ticker: str) -> None:
        """Remove a ticker from the poll set and cache."""
        ticker = ticker.upper()
        self._tickers.discard(ticker)
        self._cache.remove(ticker)

    def get_latest(self, ticker: str) -> PriceUpdate | None:
        return self._cache.get(ticker.upper())

    def get_all_latest(self) -> dict[str, PriceUpdate]:
        return self._cache.get_all()

    # ── internal ────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Poll the Massive API at regular intervals.

        Uses a single httpx.AsyncClient for connection pooling across polls.
        """
        async with httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self._request_timeout,
        ) as client:
            while True:
                if self._tickers:
                    await self._fetch_and_update(client)
                await asyncio.sleep(self._poll_interval)

    async def _fetch_and_update(self, client: httpx.AsyncClient) -> None:
        """Make one snapshot API call and update the cache.

        Error handling:
        - 429 (rate limited): log warning, skip this cycle.
        - Other HTTP errors: log error, skip this cycle.
        - Network errors: log warning, skip this cycle.

        The poll loop continues regardless — gaps in data are acceptable.
        The SSE stream simply serves stale prices until the next success.
        """
        ticker_param = ",".join(sorted(self._tickers))

        try:
            resp = await client.get(
                SNAPSHOT_PATH,
                params={"tickers": ticker_param},
            )
            resp.raise_for_status()
            data = resp.json()

            now = datetime.now(timezone.utc)
            for ticker_data in data.get("tickers", []):
                self._parse_ticker_snapshot(ticker_data, now)

        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning(
                    "Massive API rate limited (429). Skipping this poll cycle."
                )
            elif exc.response.status_code == 401:
                logger.error(
                    "Massive API authentication failed (401). Check MASSIVE_API_KEY."
                )
            else:
                logger.error(
                    "Massive API HTTP error %d: %s",
                    exc.response.status_code,
                    exc.response.text[:200],
                )
        except httpx.RequestError as exc:
            logger.warning("Massive API network error: %s", exc)

    def _parse_ticker_snapshot(
        self, ticker_data: dict, timestamp: datetime
    ) -> None:
        """Extract the price from a single ticker's snapshot object and
        update the cache.

        Expected shape (from Massive/Polygon API):
            {
              "ticker": "AAPL",
              "lastTrade": {"p": 191.25, ...},
              "prevDay": {"c": 190.00, ...},
              "todaysChange": 1.25,
              "todaysChangePerc": 0.65,
              ...
            }

        We use lastTrade.p as the current price. If lastTrade is missing
        (e.g., pre-market), fall back to day.c (today's close so far).
        """
        ticker = ticker_data.get("ticker")
        if not ticker or ticker not in self._tickers:
            return

        # Primary: last trade price
        price = None
        last_trade = ticker_data.get("lastTrade") or {}
        price = last_trade.get("p")

        # Fallback: today's close-so-far
        if price is None:
            day = ticker_data.get("day") or {}
            price = day.get("c")

        # Last resort: previous day close
        if price is None:
            prev_day = ticker_data.get("prevDay") or {}
            price = prev_day.get("c")

        if price is not None:
            self._cache.update(ticker, float(price), timestamp)
        else:
            logger.warning("No price found in snapshot for %s", ticker)
```

### Massive API response parsing detail

The snapshot endpoint returns nested objects. Here's the field priority:

| Priority | Field            | When available          |
|----------|------------------|------------------------|
| 1        | `lastTrade.p`    | During market hours     |
| 2        | `day.c`          | After first trade today |
| 3        | `prevDay.c`      | Pre-market / weekends   |

This cascade ensures we always have *some* price, even outside market hours.

---

## 9. `factory.py` — Source Selection

```python
"""Factory function that selects the market data implementation at startup.

The selection is driven by the MASSIVE_API_KEY environment variable:
- Set and non-empty → MassiveMarketData (real market data)
- Absent or empty   → SimulatorMarketData (default, no dependencies)
"""

from __future__ import annotations

import logging
import os

from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source() -> MarketDataSource:
    """Instantiate the appropriate market data source.

    Returns:
        A MarketDataSource ready to have tickers registered and start() called.
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveMarketData

        logger.info("Using Massive API for market data (poll interval: 15s)")
        return MassiveMarketData(api_key=api_key, poll_interval=15.0)
    else:
        from .simulator import SimulatorMarketData

        logger.info("Using market simulator (tick interval: 500ms)")
        return SimulatorMarketData(tick_interval=0.5)
```

---

## 10. `__init__.py` — Package Exports

```python
"""Market data package.

Public API:
    - PriceUpdate:              data model for a single price tick
    - MarketDataSource:         abstract interface (for type hints)
    - create_market_data_source: factory function
"""

from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate

__all__ = [
    "PriceUpdate",
    "MarketDataSource",
    "create_market_data_source",
]
```

---

## 11. FastAPI Integration

### 11.1 Lifespan — Startup and Shutdown

The market data source is created during app startup, seeded with the default watchlist tickers from the database, and stored on `app.state` for access by route handlers.

```python
# backend/src/app.py (relevant excerpt)

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .market import MarketDataSource, create_market_data_source


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Create the data source (simulator or Massive, based on env)
    source: MarketDataSource = create_market_data_source()

    # 2. Load watchlist tickers from SQLite and register them
    #    (database module provides this; details in DB design doc)
    tickers = get_watchlist_tickers_from_db()  # → ["AAPL", "GOOGL", ...]
    for ticker in tickers:
        source.register_ticker(ticker)

    # 3. Start the background task (tick loop or poll loop)
    await source.start()

    # 4. Store on app.state so routes can access it
    app.state.market_data = source

    yield  # ← app runs here

    # 5. Shutdown: stop the background task
    await source.stop()


app = FastAPI(lifespan=lifespan)
```

### 11.2 SSE Streaming Endpoint

```python
# backend/src/routes/stream.py

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/stream")


@router.get("/prices")
async def price_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events endpoint for live price updates.

    Pushes all tracked ticker prices every ~500ms. The client connects
    with EventSource and receives events like:

        data: {"ticker":"AAPL","price":191.25,"prev_price":190.80,...}

    The connection stays open indefinitely. EventSource auto-reconnects
    on disconnect.
    """
    source = request.app.state.market_data

    async def event_generator():
        while True:
            # Check if the client disconnected
            if await request.is_disconnected():
                break

            # Get current prices for all tracked tickers
            updates = source.get_all_latest()

            # Emit one SSE "data:" line per ticker
            for update in updates.values():
                payload = json.dumps(update.to_sse_dict())
                yield f"data: {payload}\n\n"

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering if proxied
        },
    )
```

### 11.3 Watchlist Routes — Ticker Registration

When the user adds or removes a watchlist ticker (via REST or AI chat), the route handler calls `register_ticker` / `unregister_ticker` on the market data source directly. This push-based approach means the SSE stream picks up the new ticker on the very next cycle — no database polling.

```python
# backend/src/routes/watchlist.py (relevant excerpt)

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/watchlist")


class AddTickerRequest(BaseModel):
    ticker: str


@router.post("")
async def add_to_watchlist(request: Request, body: AddTickerRequest):
    ticker = body.ticker.upper()

    # 1. Persist to SQLite
    add_ticker_to_db(ticker)

    # 2. Register with market data source (immediate SSE pickup)
    request.app.state.market_data.register_ticker(ticker)

    return {"ticker": ticker, "status": "added"}


@router.delete("/{ticker}")
async def remove_from_watchlist(request: Request, ticker: str):
    ticker = ticker.upper()

    # 1. Remove from SQLite
    remove_ticker_from_db(ticker)

    # 2. Unregister from market data source (stops SSE updates)
    request.app.state.market_data.unregister_ticker(ticker)

    return {"ticker": ticker, "status": "removed"}
```

### 11.4 Portfolio Trade — Price Lookup

When a trade is executed, the backend reads the current price from the market data source (not a database query):

```python
# backend/src/routes/portfolio.py (relevant excerpt)

@router.post("/api/portfolio/trade")
async def execute_trade(request: Request, body: TradeRequest):
    source = request.app.state.market_data

    # Get current market price
    update = source.get_latest(body.ticker.upper())
    if update is None:
        raise HTTPException(400, f"No price available for {body.ticker}")

    current_price = update.price

    # Execute at current_price...
```

---

## 12. Data Flow Diagram

```
┌─────────────────┐     register/unregister      ┌──────────────────┐
│  Watchlist API   │─────────────────────────────→│  MarketDataSource│
│  (POST/DELETE)   │                              │  (interface)     │
└─────────────────┘                               └────────┬─────────┘
                                                           │
                                              ┌────────────┴────────────┐
                                              │                         │
                                    ┌─────────▼──────────┐  ┌──────────▼─────────┐
                                    │  SimulatorMarketData│  │  MassiveMarketData │
                                    │  (GBM engine)       │  │  (REST polling)    │
                                    └─────────┬──────────┘  └──────────┬─────────┘
                                              │                         │
                                              │  cache.update()         │  cache.update()
                                              │                         │
                                              ▼                         ▼
                                    ┌───────────────────────────────────┐
                                    │           PriceCache              │
                                    │  {ticker → PriceUpdate}           │
                                    └────────┬──────────────────────────┘
                                             │
                              ┌──────────────┼──────────────┐
                              │              │              │
                              ▼              ▼              ▼
                    ┌─────────────┐ ┌──────────────┐ ┌──────────────┐
                    │ SSE Endpoint│ │ Portfolio API │ │ Trade API    │
                    │ GET /stream │ │ GET /portfolio│ │ POST /trade  │
                    │ /prices     │ │              │ │ (price lookup)│
                    └─────────────┘ └──────────────┘ └──────────────┘
                          │
                          │  Server-Sent Events
                          ▼
                    ┌─────────────┐
                    │  Frontend   │
                    │  EventSource│
                    └─────────────┘
```

---

## 13. Testing Plan

### 13.1 Unit Tests for `SimulationEngine`

```python
# backend/tests/test_engine.py

import math
from market.engine import SimulationEngine


def test_add_ticker_uses_seed_price():
    engine = SimulationEngine()
    engine.add_ticker("AAPL", seed_price=190.0)
    prices = engine.tick()
    # Price should be close to 190 after one tick (tiny dt)
    assert 180 < prices["AAPL"] < 200


def test_add_ticker_idempotent():
    engine = SimulationEngine()
    engine.add_ticker("AAPL", seed_price=100.0)
    engine.add_ticker("AAPL", seed_price=999.0)  # Should not overwrite
    prices = engine.tick()
    assert prices["AAPL"] < 200  # Still near 100, not 999


def test_remove_ticker():
    engine = SimulationEngine()
    engine.add_ticker("AAPL")
    engine.remove_ticker("AAPL")
    assert engine.tick() == {}


def test_tick_returns_all_tickers():
    engine = SimulationEngine()
    engine.add_ticker("AAPL")
    engine.add_ticker("GOOGL")
    prices = engine.tick()
    assert set(prices.keys()) == {"AAPL", "GOOGL"}


def test_prices_stay_positive():
    """Run many ticks to verify the price floor."""
    engine = SimulationEngine(tick_interval=0.5)
    engine.add_ticker("TEST", seed_price=0.05)
    for _ in range(10_000):
        prices = engine.tick()
        assert prices["TEST"] >= 0.01


def test_unknown_ticker_gets_random_seed():
    engine = SimulationEngine()
    engine.add_ticker("ZZZZ")  # Not in SEED_PRICES
    prices = engine.tick()
    assert 0.01 <= prices["ZZZZ"] <= 500  # Within reasonable range


def test_empty_engine_returns_empty():
    engine = SimulationEngine()
    assert engine.tick() == {}
```

### 13.2 Unit Tests for `PriceCache`

```python
# backend/tests/test_cache.py

from datetime import datetime, timezone
from market.cache import PriceCache


def test_first_update_direction_unchanged():
    cache = PriceCache()
    now = datetime.now(timezone.utc)
    update = cache.update("AAPL", 190.0, now)
    assert update.direction == "unchanged"
    assert update.prev_price == 190.0


def test_price_up():
    cache = PriceCache()
    now = datetime.now(timezone.utc)
    cache.update("AAPL", 190.0, now)
    update = cache.update("AAPL", 191.0, now)
    assert update.direction == "up"
    assert update.prev_price == 190.0


def test_price_down():
    cache = PriceCache()
    now = datetime.now(timezone.utc)
    cache.update("AAPL", 190.0, now)
    update = cache.update("AAPL", 189.0, now)
    assert update.direction == "down"
    assert update.prev_price == 190.0


def test_remove():
    cache = PriceCache()
    now = datetime.now(timezone.utc)
    cache.update("AAPL", 190.0, now)
    cache.remove("AAPL")
    assert cache.get("AAPL") is None
    assert len(cache) == 0


def test_get_all_returns_copy():
    cache = PriceCache()
    now = datetime.now(timezone.utc)
    cache.update("AAPL", 190.0, now)
    snapshot = cache.get_all()
    cache.update("AAPL", 195.0, now)  # Mutate cache
    assert snapshot["AAPL"].price == 190.0  # Copy is unaffected
```

### 13.3 Unit Tests for `MassiveMarketData` (Response Parsing)

```python
# backend/tests/test_massive_client.py

from datetime import datetime, timezone
from market.massive_client import MassiveMarketData


def test_parse_ticker_snapshot():
    """Verify that _parse_ticker_snapshot extracts lastTrade.p correctly."""
    source = MassiveMarketData(api_key="test-key")
    source.register_ticker("AAPL")

    snapshot = {
        "ticker": "AAPL",
        "lastTrade": {"p": 191.25, "s": 100},
        "prevDay": {"c": 190.00},
        "todaysChange": 1.25,
    }
    now = datetime.now(timezone.utc)
    source._parse_ticker_snapshot(snapshot, now)

    update = source.get_latest("AAPL")
    assert update is not None
    assert update.price == 191.25


def test_parse_ticker_snapshot_fallback_to_day_close():
    """When lastTrade is missing, fall back to day.c."""
    source = MassiveMarketData(api_key="test-key")
    source.register_ticker("AAPL")

    snapshot = {
        "ticker": "AAPL",
        "day": {"c": 189.50},
        "prevDay": {"c": 188.00},
    }
    now = datetime.now(timezone.utc)
    source._parse_ticker_snapshot(snapshot, now)

    update = source.get_latest("AAPL")
    assert update is not None
    assert update.price == 189.50


def test_parse_ignores_unregistered_ticker():
    """Tickers not in the poll set should be silently ignored."""
    source = MassiveMarketData(api_key="test-key")
    # Do NOT register AAPL

    snapshot = {"ticker": "AAPL", "lastTrade": {"p": 191.25}}
    now = datetime.now(timezone.utc)
    source._parse_ticker_snapshot(snapshot, now)

    assert source.get_latest("AAPL") is None
```

### 13.4 Integration Test for Factory

```python
# backend/tests/test_factory.py

import os
from market.factory import create_market_data_source
from market.simulator import SimulatorMarketData
from market.massive_client import MassiveMarketData


def test_default_is_simulator(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    source = create_market_data_source()
    assert isinstance(source, SimulatorMarketData)


def test_massive_when_key_set(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "pk_test_123")
    source = create_market_data_source()
    assert isinstance(source, MassiveMarketData)


def test_empty_key_uses_simulator(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "   ")
    source = create_market_data_source()
    assert isinstance(source, SimulatorMarketData)
```

---

## 14. Dependencies

Add these to `backend/pyproject.toml`:

```toml
[project]
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "httpx>=0.27.0",       # Async HTTP client for Massive API
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]
```

The simulator has **zero** external dependencies beyond the Python standard library. `httpx` is only needed for the Massive client path.

---

## 15. Configuration Summary

| Parameter | Default | Source | Notes |
|-----------|---------|--------|-------|
| `MASSIVE_API_KEY` | _(empty)_ | env var | If set, uses Massive API; otherwise simulator |
| Simulator tick interval | 500ms | hardcoded | Matches SSE push cadence |
| Massive poll interval | 15s | hardcoded | Free tier safe (5 req/min) |
| Intra-sector correlation | 0.6 | `engine.py` constant | Same-sector tickers move together |
| Event probability | 2% per tick | `engine.py` constant | Roughly once every 25 seconds |
| Event magnitude | 2-5% | `engine.py` constant | Sudden price jump for drama |
| SSE push interval | 500ms | `stream.py` | Smooth updates for UI animations |
| Price floor | $0.01 | `engine.py` constant | Prevents zero/negative prices |
| HTTP timeout (Massive) | 10s | `massive_client.py` | Per-request timeout |

---

## 16. Edge Cases and Error Handling

| Scenario | Behavior |
|----------|----------|
| Register same ticker twice | No-op (idempotent) |
| Unregister unknown ticker | No-op (idempotent) |
| Massive API returns 429 | Log warning, skip cycle, retry next interval |
| Massive API returns 401 | Log error, skip cycle (invalid key) |
| Massive API network timeout | Log warning, skip cycle, SSE serves stale prices |
| Ticker not in `SEED_PRICES` | Random price from $50–$300 range |
| SSE client disconnects | Generator exits cleanly, no server-side error |
| No tickers registered | Tick loop / poll loop runs but produces no updates |
| Price drops to near-zero | Clamped to $0.01 minimum |
| Multiple SSE clients | Each gets an independent generator reading from the same cache |
