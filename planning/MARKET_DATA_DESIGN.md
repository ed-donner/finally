# Market Data Backend — Implementation Design

Complete implementation guide for the market data subsystem in `backend/app/market/`. Covers the unified interface, GBM simulator, Massive API client, price cache, SSE streaming, and FastAPI integration. All code here is production-ready and reflects the actual implementation.

---

## Architecture

```
                ┌─────────────────────────────┐
                │   MarketDataSource (ABC)     │
                │  start / stop / add /        │
                │  remove / get_tickers        │
                └──────────┬──────────────────┘
                           │ implemented by
            ┌──────────────┴──────────────┐
            │                             │
  ┌─────────▼──────────┐      ┌──────────▼──────────┐
  │  SimulatorDataSource│      │  MassiveDataSource   │
  │  GBM @ 500ms ticks  │      │  REST poll @ 15s     │
  └─────────┬──────────┘      └──────────┬──────────┘
            │                             │
            └──────────┬──────────────────┘
                       │ writes to
                ┌──────▼──────┐
                │  PriceCache │  (thread-safe, in-memory)
                └──────┬──────┘
                       │ read by
         ┌─────────────┼─────────────┐
         │             │             │
    SSE stream    Trade exec    Portfolio
    /api/stream   /api/portfolio  snapshots
    /prices       /trade
```

**Key properties:**
- Both data sources implement the same ABC — all downstream code is source-agnostic
- Data sources push into `PriceCache`; consumers pull from it — no direct coupling
- SSE always streams at 500ms regardless of source update frequency
- Strategy selected at startup via `MASSIVE_API_KEY` env var

---

## File Structure

```
backend/
  app/
    market/
      __init__.py        # Public exports: PriceCache, create_market_data_source
      models.py          # PriceUpdate frozen dataclass
      interface.py       # MarketDataSource ABC + PriceCache
      factory.py         # create_market_data_source() — env-based strategy selection
      seed_prices.py     # Seed prices, GBM params, correlation groups
      gbm.py             # GBMSimulator (pure math, no async)
      simulator.py       # SimulatorDataSource (async wrapper around GBMSimulator)
      massive_client.py  # MassiveDataSource (async REST polling via Massive SDK)
  routes/
    stream.py            # GET /api/stream/prices (SSE endpoint)
    watchlist.py         # POST/DELETE /api/watchlist — calls market_source
    portfolio.py         # POST /api/portfolio/trade — reads price_cache
  main.py                # FastAPI app, lifespan wires everything together
```

---

## Module 1: Data Model (`models.py`)

The only data structure that leaves the market data layer. All consumers work with `PriceUpdate` objects.

```python
# backend/app/market/models.py
from dataclasses import dataclass


@dataclass(frozen=True)
class PriceUpdate:
    """Immutable snapshot of one ticker's price at one point in time."""
    ticker: str
    price: float
    previous_price: float
    timestamp: float        # Unix seconds (float)
    change: float           # price - previous_price, rounded to 4 decimal places
    direction: str          # "up", "down", or "flat"
```

---

## Module 2: Interface and Cache (`interface.py`)

### Abstract Interface

```python
# backend/app/market/interface.py
from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Abstract interface for all market data providers."""

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates. Seeds the cache with initial prices."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop producing price updates and release resources."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. Takes effect on the next poll/step."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set and evict it from the cache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of active tickers."""
```

### Price Cache

Thread-safe, in-memory store. Data sources write here; SSE, trade execution, and portfolio valuation all read from here.

```python
# backend/app/market/interface.py (continued)
import time
from threading import Lock
from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price per ticker."""

    def __init__(self):
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # monotonically increasing; used by SSE for change detection

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """
        Update the price for a ticker. Computes direction from the previous value.
        Returns the resulting PriceUpdate.
        """
        with self._lock:
            ts = timestamp or time.time()
            previous = self._prices.get(ticker)
            previous_price = previous.price if previous else price

            if price > previous_price:
                direction = "up"
            elif price < previous_price:
                direction = "down"
            else:
                direction = "flat"

            update = PriceUpdate(
                ticker=ticker,
                price=price,
                previous_price=previous_price,
                timestamp=ts,
                change=round(price - previous_price, 4),
                direction=direction,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Get the latest price for one ticker. Returns None if not yet cached."""
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: return just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        """Return a snapshot of all current prices (copied under lock)."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        """Evict a ticker from the cache (called when a ticker is removed from the watchlist)."""
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Monotonically increasing counter; increments on every price update."""
        with self._lock:
            return self._version
```

**Why a version counter?** The SSE endpoint polls the cache every 500ms. Rather than diffing the entire price dict on each iteration, it compares the version number to detect whether any prices changed since the last push. This keeps the SSE hot path O(1) in the common case.

---

## Module 3: Seed Data (`seed_prices.py`)

```python
# backend/app/market/seed_prices.py

# Starting prices for the default 10-ticker watchlist (early 2026 approximate values)
SEED_PRICES: dict[str, float] = {
    "AAPL":  190.0,
    "GOOGL": 175.0,
    "MSFT":  420.0,
    "AMZN":  185.0,
    "TSLA":  250.0,
    "NVDA":  800.0,
    "META":  500.0,
    "JPM":   195.0,
    "V":     280.0,
    "NFLX":  600.0,
}

# Per-ticker GBM parameters: sigma = annualized volatility, mu = annualized drift
# sigma intuition: 0.20 (calm blue-chip) → 0.50 (volatile growth stock)
TICKER_PARAMS: dict[str, dict] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},  # Erratic, lower drift
    "NVDA":  {"sigma": 0.40, "mu": 0.08},  # High vol, strong upward bias
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},  # Low vol (financials)
    "V":     {"sigma": 0.17, "mu": 0.04},  # Low vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Parameters for unknown tickers (not in the table above)
DEFAULT_PARAMS: dict = {"sigma": 0.25, "mu": 0.05}

# Unknown tickers start at a random price in this range
UNKNOWN_TICKER_PRICE_RANGE: tuple[float, float] = (50.0, 300.0)

# Correlation group membership (used by GBMSimulator._get_correlation)
TECH_TICKERS: frozenset[str] = frozenset({"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"})
FINANCE_TICKERS: frozenset[str] = frozenset({"JPM", "V"})

# Pairwise correlation coefficients
CORRELATIONS: dict[str, float] = {
    "intra_tech":    0.60,  # Tech stocks correlate strongly
    "intra_finance": 0.50,  # Finance stocks correlate moderately
    "cross_sector":  0.30,  # Tech ↔ finance cross-correlation
    "tsla":          0.30,  # TSLA with everything (it's a loner)
    "default":       0.30,  # Unknown ticker pairs
}
```

---

## Module 4: GBM Simulator — Pure Math (`gbm.py`)

### GBM Mathematics

At each discrete time step `dt`:

```
S(t + dt) = S(t) × exp((μ − σ²/2) × dt + σ × √dt × Z)
```

Where `Z ~ N(0,1)` is a standard normal. The `exp()` form guarantees prices can never go negative.

For 500ms ticks: `dt = 0.5 / (252 × 6.5 × 3600) ≈ 8.48e-8` (one tick as a fraction of a trading year).

### Correlated Moves via Cholesky Decomposition

To make tech stocks move together:

1. Build an `n×n` symmetric correlation matrix `C` from sector membership
2. Compute lower-triangular Cholesky factor `L` such that `C = L × Lᵀ`
3. Draw independent standard normals: `Z_ind ~ N(0,1)ⁿ`
4. Produce correlated draws: `Z_corr = L × Z_ind`
5. Use `Z_corr[i]` as the `Z` in GBM for ticker `i`

```python
# backend/app/market/gbm.py
import math
import random
import numpy as np
from .seed_prices import (
    SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS,
    UNKNOWN_TICKER_PRICE_RANGE, TECH_TICKERS, FINANCE_TICKERS, CORRELATIONS,
)


class GBMSimulator:
    """
    Generates correlated Geometric Brownian Motion price paths for multiple tickers.

    Pure math — no async, no I/O. Wrap in SimulatorDataSource for FastAPI use.
    """

    # dt = one 500ms tick as a fraction of a trading year
    # 252 trading days × 6.5 hours × 3600 seconds = 5,896,800 seconds/year
    DEFAULT_DT: float = 0.5 / (252 * 6.5 * 3600)

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,  # 0.1% per tick per ticker
    ):
        self._dt = dt
        self._event_prob = event_probability
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict] = {}
        self._tickers: list[str] = []
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self.add_ticker(ticker)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker. Seeds its price and rebuilds the correlation matrix."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(
            ticker,
            random.uniform(*UNKNOWN_TICKER_PRICE_RANGE),
        )
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker and rebuild the correlation matrix."""
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """
        Advance one time step using correlated GBM.
        Returns {ticker: new_price} for all active tickers.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        # Generate correlated standard normal draws
        z_ind = np.random.standard_normal(n)
        z = self._cholesky @ z_ind if self._cholesky is not None else z_ind

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu: float = params["mu"]
            sigma: float = params["sigma"]

            # GBM: S(t+dt) = S(t) * exp(drift_term + diffusion_term)
            drift = (mu - 0.5 * sigma ** 2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * float(z[i])
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random event: sudden 2–5% shock (~once per 500 steps per ticker)
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= (1.0 + shock)

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def get_price(self, ticker: str) -> float | None:
        """Return the current simulated price for a ticker."""
        return self._prices.get(ticker)

    def current_prices(self) -> dict[str, float]:
        """Return a copy of all current prices (initial cache seed)."""
        return dict(self._prices)

    def get_tickers(self) -> list[str]:
        """Return the list of currently active tickers."""
        return list(self._tickers)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rebuild_cholesky(self) -> None:
        """Recompute the Cholesky factor of the correlation matrix after ticker changes."""
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None  # No correlation needed for 0 or 1 ticker
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._get_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        try:
            self._cholesky = np.linalg.cholesky(corr)
        except np.linalg.LinAlgError:
            # Fallback: identity matrix (uncorrelated moves) if C is not positive definite
            self._cholesky = None

    def _get_correlation(self, t1: str, t2: str) -> float:
        """Return the pairwise Pearson correlation for two tickers based on sector."""
        if t1 == "TSLA" or t2 == "TSLA":
            return CORRELATIONS["tsla"]

        t1_tech = t1 in TECH_TICKERS
        t2_tech = t2 in TECH_TICKERS
        t1_fin = t1 in FINANCE_TICKERS
        t2_fin = t2 in FINANCE_TICKERS

        if t1_tech and t2_tech:
            return CORRELATIONS["intra_tech"]
        if t1_fin and t2_fin:
            return CORRELATIONS["intra_finance"]
        if (t1_tech and t2_fin) or (t1_fin and t2_tech):
            return CORRELATIONS["cross_sector"]

        return CORRELATIONS["default"]
```

---

## Module 5: Simulator Data Source (`simulator.py`)

Async wrapper that drives `GBMSimulator` in a background task and writes prices to `PriceCache`.

```python
# backend/app/market/simulator.py
import asyncio
import logging
from .interface import MarketDataSource, PriceCache
from .gbm import GBMSimulator

logger = logging.getLogger(__name__)


class SimulatorDataSource(MarketDataSource):
    """
    Async MarketDataSource backed by GBMSimulator.
    Ticks every `update_interval` seconds (default 500ms).
    """

    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5):
        self._cache = price_cache
        self._interval = update_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._sim: GBMSimulator | None = None

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(tickers)
        self._sim = GBMSimulator(tickers=self._tickers)
        # Seed the cache immediately — first SSE clients get prices on connect
        for ticker, price in self._sim.current_prices().items():
            self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop())
        logger.info("SimulatorDataSource started with %d tickers", len(self._tickers))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("SimulatorDataSource stopped")

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            if self._sim:
                self._sim.add_ticker(ticker)
                # Seed cache immediately so the new ticker appears in SSE without delay
                price = self._sim.get_price(ticker)
                if price is not None:
                    self._cache.update(ticker=ticker, price=price)

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _run_loop(self) -> None:
        """Main simulation loop: step GBM and write prices to cache every interval."""
        while True:
            if self._sim:
                prices = self._sim.step()
                for ticker, price in prices.items():
                    self._cache.update(ticker=ticker, price=price)
            await asyncio.sleep(self._interval)
```

---

## Module 6: Massive API Client (`massive_client.py`)

Polls the Massive (Polygon.io) REST snapshot endpoint. The SDK is synchronous, so all calls go through `asyncio.to_thread` to avoid blocking the event loop.

### Massive API Overview

| Detail | Value |
|--------|-------|
| Base URL | `https://api.massive.com` |
| Auth | `RESTClient(api_key=...)` or `MASSIVE_API_KEY` env var |
| Free tier | 5 requests/minute |
| Free tier strategy | Poll every 15s (4 req/min — safe headroom) |
| Key endpoint | `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=...` |
| One call, all tickers | Yes — entire watchlist in a single request |

### Implementation

```python
# backend/app/market/massive_client.py
import asyncio
import logging
from massive import RESTClient
from massive.rest.models import SnapshotMarketType
from .interface import MarketDataSource, PriceCache

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """
    Polls the Massive REST API for live prices and writes them to PriceCache.

    Design notes:
    - One snapshot call covers the entire watchlist (no per-ticker requests)
    - SDK is sync; wrapped in asyncio.to_thread to avoid blocking the event loop
    - Poll errors are caught and logged; the loop never crashes
    - SSE still runs at 500ms; clients see the last-known price between polls
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,  # 15s = 4 req/min (free tier safe)
    ):
        self._client = RESTClient(api_key=api_key)
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(tickers)
        # Poll once immediately so the cache is populated before the first SSE client connects
        await self._poll_once()
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("MassiveDataSource started (interval=%.1fs, tickers=%d)", self._interval, len(self._tickers))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MassiveDataSource stopped")

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            # The new ticker will be included on the next poll cycle

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Fetch snapshots for all active tickers and write to cache."""
        if not self._tickers:
            return
        try:
            snapshots = await asyncio.to_thread(
                self._client.get_snapshot_all,
                market_type=SnapshotMarketType.STOCKS,
                tickers=list(self._tickers),  # copy to avoid mutation race
            )
            for snap in snapshots:
                if snap.last_trade and snap.last_trade.price:
                    self._cache.update(
                        ticker=snap.ticker,
                        price=snap.last_trade.price,
                        timestamp=snap.last_trade.timestamp / 1000,  # SDK: ms → seconds
                    )
        except Exception as e:
            # Log and continue — the polling loop must survive transient errors
            logger.warning("Massive poll failed: %s", e)
```

### Massive Snapshot Response Fields

| Raw JSON field | SDK attribute | Description |
|---|---|---|
| `lastTrade.p` | `last_trade.price` | Most recent traded price (preferred) |
| `day.c` | `day.close` | Running session close |
| `prevDay.c` | `prev_daily_bar.close` | Previous day close (for daily % change) |
| `todaysChangePerc` | `today_change_percent` | % change vs previous close |
| `lastTrade.t` | `last_trade.timestamp` | Milliseconds UTC (raw JSON is nanoseconds) |

### Raw HTTP alternative (no SDK dependency)

```python
import requests

def get_snapshots_raw(tickers: list[str], api_key: str) -> dict[str, float]:
    """Returns {ticker: price}. Useful for debugging or environments without the SDK."""
    url = "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers"
    resp = requests.get(url, params={
        "tickers": ",".join(tickers),
        "apiKey": api_key,
    })
    resp.raise_for_status()
    data = resp.json()

    prices: dict[str, float] = {}
    for t in data.get("tickers", []):
        ticker = t["ticker"]
        price = (t.get("lastTrade") or {}).get("p") or (t.get("day") or {}).get("c")
        if price:
            prices[ticker] = float(price)
    return prices
```

---

## Module 7: Factory (`factory.py`)

```python
# backend/app/market/factory.py
import os
import logging
from .interface import MarketDataSource, PriceCache

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """
    Return a SimulatorDataSource or MassiveDataSource based on MASSIVE_API_KEY.

    If MASSIVE_API_KEY is set and non-empty → MassiveDataSource (real market data).
    Otherwise → SimulatorDataSource (built-in GBM simulator, no external dependency).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource
        logger.info("Market data: using Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        from .simulator import SimulatorDataSource
        logger.info("Market data: using GBM simulator (no MASSIVE_API_KEY set)")
        return SimulatorDataSource(price_cache=price_cache)
```

---

## Module 8: Public Exports (`__init__.py`)

```python
# backend/app/market/__init__.py
from .models import PriceUpdate
from .interface import MarketDataSource, PriceCache
from .factory import create_market_data_source

__all__ = ["PriceUpdate", "MarketDataSource", "PriceCache", "create_market_data_source"]
```

---

## SSE Streaming Endpoint (`routes/stream.py`)

The SSE endpoint reads from `PriceCache` every 500ms and pushes all current prices to connected clients. This rate is constant regardless of how often the underlying data source updates.

```python
# backend/app/routes/stream.py
import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from ..market import PriceCache

logger = logging.getLogger(__name__)
router = APIRouter()


async def _generate_events(price_cache: PriceCache) -> AsyncGenerator[str, None]:
    """
    Async generator that yields SSE-formatted events every 500ms.
    Sends all current prices each tick (not just deltas).
    Clients that briefly disconnect and reconnect immediately get a full snapshot.
    """
    while True:
        prices = price_cache.get_all()
        if prices:
            payload = {
                ticker: {
                    "ticker": p.ticker,
                    "price": p.price,
                    "previous_price": p.previous_price,
                    "change": p.change,
                    "direction": p.direction,
                    "timestamp": p.timestamp,
                }
                for ticker, p in prices.items()
            }
            yield f"data: {json.dumps(payload)}\n\n"
        await asyncio.sleep(0.5)


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Router factory — injects the shared PriceCache via closure."""

    @router.get("/api/stream/prices")
    async def stream_prices():
        """
        Long-lived SSE connection.
        Client uses native EventSource API; reconnection is automatic.
        """
        return StreamingResponse(
            _generate_events(price_cache),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",   # Prevent nginx from buffering SSE frames
                "Connection": "keep-alive",
            },
        )

    return router
```

### Frontend `EventSource` usage

```typescript
// frontend/src/hooks/usePriceStream.ts
const source = new EventSource("/api/stream/prices");

source.onmessage = (event) => {
  const prices = JSON.parse(event.data);
  // prices = { AAPL: { ticker, price, previous_price, change, direction, timestamp }, ... }
  dispatch({ type: "PRICES_UPDATED", payload: prices });
};

source.onerror = () => {
  // EventSource reconnects automatically — no manual retry needed
  console.warn("SSE connection lost; browser will retry");
};
```

---

## FastAPI Integration (`main.py`)

### App Lifespan — Wiring Everything Together

```python
# backend/app/main.py
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from .market import PriceCache, create_market_data_source
from .db import get_watchlist_tickers  # returns list[str] for user "default"
from .routes.stream import create_stream_router
from .routes import watchlist, portfolio, chat

logger = logging.getLogger(__name__)

# Module-level singletons — shared across all requests
price_cache = PriceCache()
market_source = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: seed cache and start background data source. Shutdown: stop it cleanly."""
    global market_source

    initial_tickers = await get_watchlist_tickers(user_id="default")
    market_source = create_market_data_source(price_cache)
    await market_source.start(initial_tickers)
    logger.info("Market data started with tickers: %s", initial_tickers)

    yield  # App is running

    await market_source.stop()
    logger.info("Market data stopped")


app = FastAPI(lifespan=lifespan)

# Mount routers
app.include_router(create_stream_router(price_cache))
app.include_router(watchlist.create_router(price_cache, lambda: market_source))
app.include_router(portfolio.create_router(price_cache))
app.include_router(chat.router)

# Serve Next.js static export last (catch-all)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
```

### Watchlist Endpoints

```python
# backend/app/routes/watchlist.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from ..market import PriceCache
from .. import db

router = APIRouter()


class AddTickerRequest(BaseModel):
    ticker: str


def create_router(price_cache: PriceCache, get_source) -> APIRouter:

    @router.get("/api/watchlist")
    async def get_watchlist():
        """Return current watchlist tickers with their latest prices."""
        tickers = await db.get_watchlist_tickers(user_id="default")
        result = []
        for ticker in tickers:
            update = price_cache.get(ticker)
            result.append({
                "ticker": ticker,
                "price": update.price if update else None,
                "change": update.change if update else None,
                "direction": update.direction if update else None,
            })
        return result

    @router.post("/api/watchlist")
    async def add_ticker(body: AddTickerRequest):
        ticker = body.ticker.upper().strip()
        if not ticker:
            raise HTTPException(400, "ticker is required")
        await db.add_watchlist_ticker(user_id="default", ticker=ticker)
        source = get_source()
        if source:
            await source.add_ticker(ticker)
        return {"ticker": ticker}

    @router.delete("/api/watchlist/{ticker}")
    async def remove_ticker(ticker: str):
        ticker = ticker.upper().strip()
        await db.remove_watchlist_ticker(user_id="default", ticker=ticker)
        source = get_source()
        if source:
            await source.remove_ticker(ticker)
        return {"ticker": ticker}

    return router
```

### Trade Execution — Reads from Cache

```python
# backend/app/routes/portfolio.py (trade endpoint excerpt)
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from ..market import PriceCache
from .. import db

class TradeRequest(BaseModel):
    ticker: str
    quantity: float
    side: str  # "buy" or "sell"


def create_router(price_cache: PriceCache) -> APIRouter:
    router = APIRouter()

    @router.post("/api/portfolio/trade")
    async def execute_trade(body: TradeRequest):
        if body.side not in ("buy", "sell"):
            raise HTTPException(400, "side must be 'buy' or 'sell'")
        if body.quantity <= 0:
            raise HTTPException(400, "quantity must be positive")

        update = price_cache.get(body.ticker.upper())
        if not update:
            raise HTTPException(400, f"No price available for {body.ticker}")

        current_price = update.price
        profile = await db.get_user_profile(user_id="default")

        if body.side == "buy":
            cost = current_price * body.quantity
            if profile.cash_balance < cost:
                raise HTTPException(400, f"Insufficient cash: need ${cost:.2f}, have ${profile.cash_balance:.2f}")
            await db.execute_buy(user_id="default", ticker=body.ticker, quantity=body.quantity, price=current_price)

        elif body.side == "sell":
            position = await db.get_position(user_id="default", ticker=body.ticker)
            if not position or position.quantity < body.quantity:
                owned = position.quantity if position else 0
                raise HTTPException(400, f"Insufficient shares: need {body.quantity}, have {owned}")
            await db.execute_sell(user_id="default", ticker=body.ticker, quantity=body.quantity, price=current_price)

        return {"ticker": body.ticker, "side": body.side, "quantity": body.quantity, "price": current_price}

    return router
```

---

## Application Lifecycle Summary

| Event | What Happens |
|-------|-------------|
| App startup | `PriceCache` created; `create_market_data_source()` selects impl; `source.start(watchlist_tickers)` seeds cache and spawns background task |
| SSE client connects | Gets full current cache snapshot on first message; then receives updates every 500ms |
| User adds ticker | `source.add_ticker(t)` → cache seeded immediately → appears in next SSE frame |
| User removes ticker | `source.remove_ticker(t)` → evicted from cache → disappears from SSE |
| Trade executed | `price_cache.get(ticker).price` always reflects latest price |
| Portfolio snapshot (background task) | `price_cache.get_all()` values all positions |
| App shutdown | `source.stop()` cancels background task cleanly |

---

## Behavior Differences: Simulator vs. Massive

| Aspect | Simulator | Massive API |
|--------|-----------|-------------|
| Update frequency | 500ms (every tick) | 15s (free) / 2–5s (paid) |
| SSE cadence | 500ms | 500ms (re-emits last-known price between polls) |
| Starting prices | Hardcoded seed values | Real last-traded price |
| New tickers | Random $50–$300 | Real last-traded price |
| After-hours | Always "live" | May show stale prices |
| External dependency | None | Internet + API key |
| Rate limits | None | 5 req/min (free) |
| History available | No | Yes (via aggregate bars endpoint) |

---

## Error Handling

### Massive Polling Errors

```python
# Behavior per HTTP status code
# 401 Unauthorized  → invalid API key; log error, continue polling (key may be corrected)
# 403 Forbidden     → plan doesn't support endpoint; log, consider falling back to sim
# 429 Too Many Req  → rate limit hit; log warning, backing off happens naturally via sleep
# 5xx Server Error  → SDK retries 3× automatically; if all fail, log and continue loop
```

The golden rule: **the polling loop must never crash**. Every `_poll_once` call is wrapped in `try/except Exception`. Individual poll failures mean a 15s stale gap at worst — not a service outage.

### Simulator Cholesky Fallback

If the correlation matrix is not positive semi-definite (unusual ticker combinations), `_rebuild_cholesky` catches `np.linalg.LinAlgError` and sets `self._cholesky = None`. The simulator continues with uncorrelated (independent) moves rather than crashing.

---

## Testing Strategy

### Unit tests — `backend/tests/market/`

```
test_models.py           — PriceUpdate construction, direction logic, frozen dataclass
test_cache.py            — thread-safety, update/get/get_all/remove, version counter
test_simulator.py        — GBMSimulator: step(), add/remove ticker, Cholesky rebuild, event probability
test_simulator_source.py — SimulatorDataSource: start/stop, add/remove ticker, cache seeded on start
test_factory.py          — env var logic (with/without MASSIVE_API_KEY)
test_massive.py          — MassiveDataSource: _poll_once(), error handling, add/remove ticker
```

### Example test patterns

```python
# test_cache.py
def test_direction_computed_on_update():
    cache = PriceCache()
    cache.update("AAPL", 100.0)
    update = cache.update("AAPL", 101.0)
    assert update.direction == "up"
    assert update.change == 1.0

def test_version_increments_on_update():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 100.0)
    assert cache.version == v0 + 1

def test_remove_evicts_ticker():
    cache = PriceCache()
    cache.update("AAPL", 100.0)
    cache.remove("AAPL")
    assert cache.get("AAPL") is None


# test_massive.py — mocking the SDK
from unittest.mock import MagicMock, patch

@pytest.mark.asyncio
async def test_poll_once_updates_cache():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=60)

    # Build a mock snapshot object
    mock_snap = MagicMock()
    mock_snap.ticker = "AAPL"
    mock_snap.last_trade.price = 190.0
    mock_snap.last_trade.timestamp = 1700000000000  # ms

    source._client = MagicMock()
    source._client.get_snapshot_all.return_value = [mock_snap]

    await source.start(["AAPL"])
    update = cache.get("AAPL")

    assert update is not None
    assert update.price == 190.0
    assert update.ticker == "AAPL"
```

---

## Usage — Downstream Code Reference

```python
from app.market import PriceCache, create_market_data_source

# -- Startup (in lifespan) --
cache = PriceCache()
source = create_market_data_source(cache)   # reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", "MSFT", ...])

# -- Reading prices --
update = cache.get("AAPL")         # PriceUpdate | None
price  = cache.get_price("AAPL")   # float | None
all_px = cache.get_all()           # dict[str, PriceUpdate]

# -- Dynamic watchlist --
await source.add_ticker("PYPL")
await source.remove_ticker("NFLX")

# -- Shutdown --
await source.stop()
```
