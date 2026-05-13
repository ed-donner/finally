# Market Data Backend — Design

Detailed design for the FinAlly market data subsystem. Defines a unified abstract interface that two interchangeable implementations (a GBM-based simulator and the Massive / Polygon.io REST poller) plug into, plus the shared price cache and SSE streaming endpoint that the rest of the application consumes.

This is a self-contained design — implementing it produces a fully working `backend/app/market/` package that the portfolio, trading, and chat subsystems can build against without further changes.

---

## 1. Goals and Constraints

| Requirement | Implication |
|---|---|
| Two data sources behind one interface | Abstract base class + strategy pattern + factory |
| Simulator works with no external dependencies | GBM math only requires `numpy`; no API keys |
| Massive API is optional | Selected via `MASSIVE_API_KEY` env var; no hard runtime dependency on a key |
| ~500ms update cadence to the frontend | Simulator ticks every 500ms; Massive polls slower (15s on free tier) but the cache pushes stale data unchanged |
| Single-user today, multi-user later | All consumers read from a shared in-process cache; no per-connection producer state |
| Cleanly testable | No globals; cache and source are dependency-injected; both sources are unit-testable without the network |

**Non-goals.** Order books, limit orders, options chains, news feeds, fundamentals, or pre/post-market segmentation. Market orders only; one price per ticker; that's the whole contract.

---

## 2. Architecture

```
                ┌──────────────────────────────────────┐
                │   MarketDataSource (ABC)             │
                │   start/stop/add_ticker/             │
                │   remove_ticker/get_tickers          │
                └─────────────┬────────────────────────┘
                              │ implemented by
              ┌───────────────┴────────────────┐
              ▼                                ▼
   ┌──────────────────────┐         ┌──────────────────────┐
   │ SimulatorDataSource  │         │ MassiveDataSource    │
   │  - GBMSimulator      │         │  - massive.RESTClient│
   │  - 500ms loop        │         │  - 15s poll loop     │
   └──────────┬───────────┘         └──────────┬───────────┘
              │                                │
              └────────── writes to ───────────┘
                              │
                              ▼
                  ┌────────────────────────┐
                  │      PriceCache        │
                  │  ticker -> PriceUpdate │
                  │  (thread-safe, +ver)   │
                  └──────────┬─────────────┘
                             │ reads
            ┌────────────────┼─────────────────┐
            ▼                ▼                 ▼
   ┌────────────────┐ ┌──────────────┐ ┌─────────────────┐
   │ SSE endpoint   │ │ Portfolio    │ │ Trade execution │
   │ /api/stream/   │ │ valuation    │ │ uses cache for  │
   │   prices       │ │ uses cache   │ │ fill price      │
   └────────────────┘ └──────────────┘ └─────────────────┘
```

**Key idea — the cache is the seam.** Producers (simulator or Massive poller) push prices into the cache on their own schedule. Consumers (SSE, portfolio, trades) read from the cache. The two sides never call each other directly. This makes the data source completely swappable and the cache trivially mockable for tests.

---

## 3. Directory Layout

```
backend/app/market/
├── __init__.py            # Re-exports the public surface
├── models.py              # PriceUpdate dataclass
├── interface.py           # MarketDataSource ABC
├── cache.py               # PriceCache (thread-safe, versioned)
├── seed_prices.py         # Constants: SEED_PRICES, TICKER_PARAMS, correlations
├── simulator.py           # GBMSimulator + SimulatorDataSource
├── massive_client.py      # MassiveDataSource (Polygon.io / Massive)
├── factory.py             # create_market_data_source()
└── stream.py              # SSE FastAPI router
```

`__init__.py` exports the names downstream code needs:

```python
# backend/app/market/__init__.py
from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = [
    "MarketDataSource",
    "PriceCache",
    "PriceUpdate",
    "create_market_data_source",
    "create_stream_router",
]
```

---

## 4. Core Data Model

`PriceUpdate` is the only type that crosses the market-data boundary. Frozen dataclass with `slots=True` for cheap allocation on the hot path. Derived fields (`change`, `direction`) are computed properties so they can't drift out of sync with `price`/`previous_price`.

```python
# backend/app/market/models.py
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        if self.price > self.previous_price:
            return "up"
        if self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
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

Notes:
- `change` and `direction` are derived; the cache stores only `price`, `previous_price`, `timestamp`. This keeps invariants intact.
- `to_dict()` is the serialization contract for the SSE payload. The frontend EventSource handler depends on these field names.

---

## 5. The Abstract Interface

`MarketDataSource` is a five-method ABC. Both implementations follow exactly the same lifecycle.

```python
# backend/app/market/interface.py
from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source for prices — it
    reads from the cache.

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
        """Begin producing price updates. Must be called exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker; also removes it from the PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

Design choices:
- The interface returns no prices. It owns a background task; consumers read the cache.
- `start()` is async so subclasses can do an immediate first poll (Massive does this so the cache isn't empty for the first 15 seconds).
- `add_ticker` / `remove_ticker` are async even though both current implementations have synchronous bodies — keeps room for sources that need an API call to subscribe.

---

## 6. The Price Cache

Single source of truth for current prices. Thread-safe (the Massive client is synchronous and runs in `asyncio.to_thread`, so writes can come off the event loop). Carries a monotonic `version` counter so the SSE stream can skip transmissions when nothing changed.

```python
# backend/app/market/cache.py
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one at a time).
    Readers: SSE endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Bumped on every update; used for SSE change detection

    def update(
        self, ticker: str, price: float, timestamp: float | None = None
    ) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        Direction and change are derived from the previous cache entry.
        First update for a ticker has previous_price == price (direction='flat').
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
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

Why a version counter? The SSE loop wakes every 500ms. If the cache hasn't changed since the last send (Massive only updates every 15s, so 29 out of 30 SSE ticks have no new data), we skip the JSON serialization and the network frame entirely.

Why a `Lock` and not an `asyncio.Lock`? Because the Massive client is synchronous and runs in `asyncio.to_thread`, which means writes happen off the event loop. `threading.Lock` is the right primitive for cross-thread mutual exclusion. Read paths (SSE, portfolio, trade) all run on the event loop and hold the lock briefly — microseconds. There's no risk of starving the loop.

---

## 7. Seed Data and GBM Parameters

Constants used by the simulator. Realistic starting prices and per-ticker volatility / drift, plus the sector groupings used to build the correlation matrix.

```python
# backend/app/market/seed_prices.py

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

# Annualized GBM parameters per ticker.
#   sigma: volatility (higher = wider price swings)
#   mu:    drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},  # High vol
    "NVDA":  {"sigma": 0.40, "mu": 0.08},  # High vol, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},  # Low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},  # Low vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR    = 0.6  # Tech stocks move together
INTRA_FINANCE_CORR = 0.5  # Finance stocks move together
CROSS_GROUP_CORR   = 0.3  # Between sectors / unknown tickers
TSLA_CORR          = 0.3  # TSLA does its own thing
```

Tickers added dynamically that aren't in `SEED_PRICES` get a random starting price in `[$50, $300]` and the `DEFAULT_PARAMS`.

---

## 8. The Simulator

Two classes: `GBMSimulator` does the math, `SimulatorDataSource` wraps it in an async loop and implements the `MarketDataSource` interface.

### 8.1 GBM Math

At each step every ticker evolves as:

```
S(t + dt) = S(t) · exp((μ − σ²/2) · dt + σ · √dt · Z)
```

- `μ`, `σ` are the ticker's annualized drift and volatility.
- `dt` is the time step expressed as a fraction of a trading year. For 500ms ticks over 252 trading days × 6.5 hours/day × 3600 sec/hour = 5,896,800 trading seconds/year, that gives `dt ≈ 8.48e-8`.
- `Z` is a correlated standard normal draw (see §8.2).

This is the same math that underlies Black-Scholes. Prices are log-normal — they can't go negative (good — `exp` is always positive) and they exhibit realistic intraday wiggle.

### 8.2 Correlated Moves via Cholesky

Real stocks don't move independently. The simulator builds an `n × n` correlation matrix `C` from the sector groupings in §7, computes its Cholesky factor `L` (so `L · Lᵀ = C`), draws `n` independent standard normals `z_indep`, and uses `z_corr = L · z_indep` as the per-ticker draws.

The correlation matrix is rebuilt when tickers are added or removed (O(n²), but n is tiny — under 50). With one or zero tickers, no Cholesky is needed.

### 8.3 Random Shock Events

Every step, every ticker has a ~0.1% chance of a sudden 2-5% move (random sign). With 10 tickers ticking twice per second, expect a visible shock roughly every 50 seconds — enough to keep the dashboard interesting without destabilizing prices.

### 8.4 Implementation

```python
# backend/app/market/simulator.py
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
    """Geometric Brownian Motion simulator for correlated stock prices."""

    # 500ms expressed as fraction of a trading year
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability

        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    # --- Public API ---

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Returns {ticker: new_price}.

        Hot path — called every 500ms. Keep it fast.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        # n independent standard normals -> n correlated draws via Cholesky
        z_indep = np.random.standard_normal(n)
        z_corr = self._cholesky @ z_indep if self._cholesky is not None else z_indep

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu = self._params[ticker]["mu"]
            sigma = self._params[ticker]["sigma"]

            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_corr[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random shock event
            if random.random() < self._event_prob:
                magnitude = random.uniform(0.02, 0.05)
                sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + magnitude * sign
                logger.debug(
                    "Shock event on %s: %.1f%% %s",
                    ticker, magnitude * 100, "up" if sign > 0 else "down",
                )

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add a ticker without rebuilding Cholesky (used for batch init)."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        # TSLA is technically in tech but moves independently
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR


class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator.

    Runs an asyncio task that calls GBMSimulator.step() every `update_interval`
    seconds and writes results to the PriceCache.
    """

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed the cache so SSE has data on the very first frame
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(tickers))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Simulator stopped")

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
            logger.info("Simulator: added ticker %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
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

Behavior notes:
- Prices never go negative (GBM is multiplicative through `exp`).
- The tiny `dt` produces sub-cent moves per tick, which accumulate over time into realistic intraday ranges. TSLA at σ=0.50 produces roughly the right daily volatility envelope; V at σ=0.17 looks like a real payments stock.
- `_run_loop` catches all exceptions and continues — a transient `numpy` error in one step shouldn't kill the whole price feed.

---

## 9. The Massive (Polygon.io) Implementation

Massive is the rebrand of the Polygon.io REST API. We poll the snapshot endpoint, which returns prices for a list of tickers in a single HTTP call — critical for staying inside the free-tier rate limit.

### 9.1 Endpoint Used

`GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,...`

One call, N tickers, one response. The Python SDK wraps this as:

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="...")
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT"],
)
for snap in snapshots:
    print(snap.ticker, snap.last_trade.price, snap.last_trade.timestamp)  # ms
```

### 9.2 Rate Limits and Cadence

| Tier | Limit | Poll interval used |
|---|---|---|
| Free | 5 req/min | 15s |
| Paid | unlimited (de facto) | 2-5s |

We default to 15s — safe on every tier. The `poll_interval` parameter is configurable for users on paid plans.

### 9.3 Async/Sync Bridging

The Massive `RESTClient` is synchronous. To avoid blocking the FastAPI event loop, every call to it is wrapped with `asyncio.to_thread(...)`. This runs the call in the default thread executor while the event loop keeps serving SSE clients and API requests.

### 9.4 Error Handling

The poll loop wraps each cycle in `try/except`. Network errors, 429 rate-limit errors, and malformed payloads are logged and swallowed; the loop sleeps and retries on the next cycle. Critically: **a transient API failure must not kill the data feed**. Stale prices in the cache are better than empty.

Per-snapshot parsing is also guarded: if a single ticker's response is malformed (missing `last_trade`, e.g.), we skip just that ticker.

### 9.5 Implementation

```python
# backend/app/market/massive_client.py
from __future__ import annotations

import asyncio
import logging

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call, then writes results to the PriceCache.

    Rate limits:
      Free tier: 5 req/min -> poll every 15s (default)
      Paid:      higher    -> poll every 2-5s
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)

        # Immediate first poll so the cache has data right away
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval",
            len(tickers), self._interval,
        )

    async def stop(self) -> None:
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
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed %s", ticker)

    def get_tickers(self) -> list[str]:
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
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    # Massive timestamps are Unix ms -> convert to seconds
                    timestamp = snap.last_trade.timestamp / 1000.0
                    self._cache.update(
                        ticker=snap.ticker, price=price, timestamp=timestamp,
                    )
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s",
                        getattr(snap, "ticker", "???"), e,
                    )
            logger.debug(
                "Massive poll: updated %d/%d tickers", processed, len(self._tickers),
            )

        except Exception as e:
            # Common failures: 401 bad key, 429 rate limit, network errors.
            # Don't re-raise — the loop retries on the next interval.
            logger.error("Massive poll failed: %s", e)

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### 9.6 Snapshot Response Shape (Reference)

The fields we depend on:

```json
{
  "ticker": "AAPL",
  "last_trade": {
    "price": 190.42,
    "size": 100,
    "timestamp": 1675190399000
  },
  "day": {
    "previous_close": 188.10,
    "change": 2.32,
    "change_percent": 1.23
  }
}
```

We only consume `ticker`, `last_trade.price`, and `last_trade.timestamp`. The rest is available for future detail views but isn't part of the live feed contract.

---

## 10. The Factory

Picks the right implementation based on the environment, no other logic. Lives in its own module so test code can monkey-patch the env var without touching the implementations.

```python
# backend/app/market/factory.py
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment variables.

    - MASSIVE_API_KEY set and non-empty -> MassiveDataSource (real data)
    - Otherwise                         -> SimulatorDataSource (GBM)

    Returns an unstarted source; caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)

    logger.info("Market data source: GBM Simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

Whitespace-only or empty `MASSIVE_API_KEY` values are explicitly treated as unset — the `.strip()` matters because Docker `--env-file` parsing can leave trailing whitespace.

---

## 11. SSE Streaming Endpoint

The SSE endpoint is the primary consumer of the price cache. One endpoint, one route, a streaming response that emits all cached prices on a 500ms cadence.

### 11.1 Protocol

Each SSE frame is:

```
data: {"AAPL": {"ticker":"AAPL","price":190.42,"previous_price":190.40,...}, "GOOGL": {...}, ...}\n\n
```

A `retry: 1000\n\n` directive is sent on connect; this tells the browser to wait 1 second before auto-reconnecting after a disconnect (built-in `EventSource` behavior).

### 11.2 Skip-When-Unchanged Optimization

The cache exposes a `version` counter that increments on every update. The SSE generator wakes every 500ms but only emits a frame when the version has changed. When the data source is Massive (15s poll), 29 of 30 wake-ups become no-ops.

### 11.3 Client Disconnect

`request.is_disconnected()` is checked on every wake-up. When a client closes the EventSource, the generator exits cleanly and the streaming task is collected.

### 11.4 Headers

- `Cache-Control: no-cache` — never cache an event stream.
- `Connection: keep-alive` — implicit for SSE but explicit doesn't hurt.
- `X-Accel-Buffering: no` — disables nginx response buffering if FastAPI is ever fronted by a proxy. Without this, nginx will hold frames until its buffer fills.

### 11.5 Implementation

```python
# backend/app/market/stream.py
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create the SSE router. Injects the PriceCache without globals."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint for live price updates.

        Streams the full snapshot of cached prices every ~500ms (skipping
        frames when nothing has changed). Frontend connects via EventSource
        and receives:

            data: {"AAPL": {...PriceUpdate.to_dict()...}, ...}
        """
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Async generator yielding SSE-formatted price events."""
    yield "retry: 1000\n\n"

    last_version = -1
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()
                if prices:
                    data = {t: u.to_dict() for t, u in prices.items()}
                    yield f"data: {json.dumps(data)}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

---

## 12. Application Wiring

How `main.py` (or wherever the FastAPI app is constructed) brings it all together. The cache and source are created at startup and stored on `app.state`; the SSE router is mounted; cleanup happens in the shutdown handler.

```python
# backend/app/main.py (relevant excerpt)
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import (
    PriceCache,
    create_market_data_source,
    create_stream_router,
)

DEFAULT_TICKERS = [
    "AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
    "NVDA", "META", "JPM", "V", "NFLX",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    cache = PriceCache()
    source = create_market_data_source(cache)
    # In real wiring, pull initial tickers from the watchlist table instead
    await source.start(DEFAULT_TICKERS)

    app.state.price_cache = cache
    app.state.market_source = source

    yield

    # Shutdown
    await source.stop()


app = FastAPI(lifespan=lifespan)
# The SSE router needs the cache, so mount it after the cache exists.
# Easiest: build the router at startup inside lifespan, or use a closure.
# Below uses a module-level closure pattern:

_cache_holder: dict = {}

def _get_cache() -> PriceCache:
    return _cache_holder["cache"]

# ... or, cleaner, do this inline once you have the cache:
#     app.include_router(create_stream_router(cache))
```

In practice the cleanest pattern is to include the router from inside `lifespan` before the `yield`, or to pass the cache to a setup function that registers the router. The exact wiring is up to the backend/integration agent; the contract is just that `create_stream_router(cache)` returns a `APIRouter` mounted under `/api/stream`.

### 12.1 Reading prices elsewhere

Other modules (portfolio valuation, trade execution) take a `PriceCache` reference:

```python
# Portfolio valuation
def total_value(cash: float, positions: list[Position], cache: PriceCache) -> float:
    total = cash
    for pos in positions:
        price = cache.get_price(pos.ticker)
        if price is not None:
            total += price * pos.quantity
    return total


# Trade execution
def execute_buy(
    ticker: str, quantity: float, cash: float, cache: PriceCache,
) -> tuple[float, float]:
    price = cache.get_price(ticker)
    if price is None:
        raise ValueError(f"No price for {ticker}")
    cost = price * quantity
    if cost > cash:
        raise ValueError("Insufficient cash")
    return price, cost
```

Neither caller knows whether the price came from the simulator or Massive. That's the point of the design.

### 12.2 Watchlist add/remove

The watchlist API endpoints call into the market data source:

```python
@app.post("/api/watchlist")
async def add_to_watchlist(req: AddTickerRequest):
    # ... insert into watchlist table ...
    await app.state.market_source.add_ticker(req.ticker)
    return {"ok": True}


@app.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(ticker: str):
    # ... delete from watchlist table ...
    await app.state.market_source.remove_ticker(ticker)
    return {"ok": True}
```

---

## 13. Dependencies

Add to `backend/pyproject.toml`:

```toml
[project]
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "numpy>=1.26",
    "massive>=1.0.0",   # Polygon.io / Massive client (top-level import is safe; ~6MB)
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "pytest-cov>=4",
    "ruff>=0.4",
    "httpx>=0.27",   # For SSE integration tests
]

[tool.hatch.build.targets.wheel]
packages = ["app"]   # REQUIRED — hatchling won't auto-detect otherwise
```

`numpy` is required even when Massive is used (the simulator module imports it). `massive` is required even when only the simulator is active (the package imports it at module level so the factory is a fast lookup, not a lazy import that explodes 200 lines deep). Both are small.

---

## 14. Testing Strategy

Six test modules under `backend/tests/market/`:

| Module | Coverage target | What it tests |
|---|---|---|
| `test_models.py` | `PriceUpdate` immutability, derived properties, `to_dict()` keys |
| `test_cache.py` | Update/get/remove, version counter, `get_all` returns copies, thread-safety smoke test with `concurrent.futures.ThreadPoolExecutor` |
| `test_simulator.py` | GBM math doesn't blow up over N steps, prices stay positive, Cholesky succeeds for the full 10-ticker default set, add/remove rebuilds matrix correctly |
| `test_simulator_source.py` | Async lifecycle: start populates cache, step loop runs, stop cancels cleanly, add/remove propagates |
| `test_massive.py` | Mock the `RESTClient` (patch with `create=True` since lazy import patterns are fragile) — verify `_poll_once` writes to cache, timestamp conversion is ms->s, malformed snapshot is skipped not fatal, `stop()` cancels the task |
| `test_factory.py` | Env var present -> Massive; absent / whitespace -> Simulator |
| `test_stream.py` | Using `httpx.AsyncClient` against the FastAPI app, connect and read a few SSE frames; verify `retry: 1000` directive and that no frame is sent when version doesn't change |

Conventions:
- `pytest-asyncio` in `asyncio_mode = "auto"` so test functions don't need decorators.
- Each test that starts a `MarketDataSource` finalizes with `await source.stop()` (use a fixture).
- Massive tests use `patch("app.market.massive_client.RESTClient", create=True)` so they pass even if the `massive` package isn't installed in the test environment.

Coverage target: 80%+ overall. `massive_client.py` will hover around 55% because real API calls aren't exercised — that's expected and acceptable.

---

## 15. Logging

All modules use `logger = logging.getLogger(__name__)` so they appear under `app.market.*` in the logger hierarchy. The app's logging config should set `app.market` to `INFO` in production and `DEBUG` in development.

What's logged at each level:

| Level | Event |
|---|---|
| INFO  | Source started/stopped, ticker add/remove, SSE client connect/disconnect |
| DEBUG | Per-poll cycle stats, individual shock events |
| WARNING | Malformed Massive snapshot (single ticker skipped) |
| ERROR | Massive poll failure (whole cycle), simulator step exception |

No logging from the hot path (`GBMSimulator.step` per-ticker, `PriceCache.update`) — those run 20+ times per second and would dominate the log.

---

## 16. Implementation Order

A sensible build order so each step is testable in isolation:

1. **`models.py`** — `PriceUpdate`. Tests: instantiate, properties, `to_dict`.
2. **`cache.py`** — `PriceCache`. Tests: update, get, version, remove, threading smoke.
3. **`interface.py`** — `MarketDataSource` ABC. No tests needed.
4. **`seed_prices.py`** — constants. No tests.
5. **`simulator.py`** — `GBMSimulator` first (synchronous, easy to test), then `SimulatorDataSource` (async wrapper). Tests for both.
6. **`massive_client.py`** — `MassiveDataSource` with mocked `RESTClient`.
7. **`factory.py`** — `create_market_data_source`. Tests with env var manipulation.
8. **`stream.py`** — SSE router. Integration test with `httpx.AsyncClient`.
9. **`__init__.py`** — public re-exports.
10. **Wire into `app/main.py` lifespan.**

After step 5 alone, the simulator is a working demo (the `market_data_demo.py` Rich script can prove it). After step 8, the SSE stream is consumable by a curl-or-browser client. After step 10, the rest of the app can read prices.

---

## 17. Operational Notes

- **First-tick latency.** The simulator seeds the cache during `start()`, so the SSE client sees data on its very first frame. The Massive source does an immediate first poll inside `start()` for the same reason — without it, the cache would be empty for up to 15 seconds after boot.
- **Memory.** `PriceCache` holds one `PriceUpdate` per ticker. With `slots=True` that's ~200 bytes per ticker. Even 1,000 tickers is 200 KB. No history is retained.
- **CPU.** GBM step for 10 tickers is microseconds. Cholesky on a 50×50 matrix is sub-millisecond and only runs on add/remove. The hot loop is bound by `asyncio.sleep(0.5)` not by compute.
- **Failure modes.**
  - Massive API down → cache holds last-known prices; loop retries every 15s; frontend sees no flashing but stale numbers (acceptable).
  - Bad API key → 401 logged on every poll; cache stays seeded with whatever the first poll captured (probably nothing). To handle this gracefully, the factory could optionally validate the key on startup, but doing so adds a fragile network call to boot. Current design: just fail to receive prices and log loudly.
  - Network flapping → poll catches `Exception` and continues. No retry-with-backoff is needed because we already only poll once per interval.
  - Simulator step exception → caught and logged; next tick continues. Prices freeze for one frame, no cascade.

---

## 18. Future Extensions

Not part of this build, but the design accommodates them cleanly:

- **Historical bars** for the detail-view chart. Add a third method to `MarketDataSource` like `async def history(ticker, bars=200) -> list[Bar]`. Simulator can re-derive on demand from `(seed_price, mu, sigma)`; Massive uses `client.list_aggs(...)`.
- **Per-client filtering.** SSE currently broadcasts all tickers to every client. With multi-user, swap `price_cache.get_all()` for a per-user filter pulled from the watchlist table.
- **Replacement data source.** Yahoo Finance, IEX Cloud, Alpaca — any provider — slots in as a third `MarketDataSource` implementation; the factory grows a new branch. Nothing else changes.

---

## 19. Summary

The market data subsystem is eight small modules totaling ~500 lines. Two implementations of one ABC, a shared cache, a factory, and an SSE endpoint. The math is GBM with Cholesky-correlated noise for the simulator and the standard `/v2/snapshot` Polygon call for real data. Everything downstream — portfolio, trades, frontend — reads from the cache and is completely unaware of which source is running.

The result is a clean seam: swap data sources with an env var, mock the cache for tests, and add new providers without touching anything outside `app/market/`.
