# Market Data Backend — Complete Implementation Design

## Overview

The market data subsystem is the engine behind all live price data in FinAlly. It has three jobs:

1. **Produce prices** — either by simulating GBM price dynamics locally, or by polling the Massive (formerly Polygon.io) REST API.
2. **Cache prices** — maintain an in-memory store of the latest price for every tracked ticker, shared by SSE streaming and API routes.
3. **Push prices** — fan out every update to all connected SSE clients via per-client asyncio queues.

All downstream code (SSE, portfolio routes, watchlist routes) reads from the cache. Nothing calls the data source directly.

---

## Directory Structure

```
backend/app/market/
├── __init__.py          # public re-exports
├── models.py            # PriceUpdate, DailyBar dataclasses
├── cache.py             # PriceCache — shared state + SSE fan-out
├── interface.py         # MarketDataSource abstract base class
├── simulator.py         # MarketSimulator — GBM synthetic prices
├── massive_client.py    # MassiveClient — polls Massive REST API
└── factory.py           # create_market_data_source() factory
```

---

## 1. Data Models

**File:** `backend/app/market/models.py`

```python
from dataclasses import dataclass
from datetime import datetime


@dataclass
class PriceUpdate:
    ticker: str
    price: float
    prev_price: float
    timestamp: datetime
    change: float        # absolute dollar change (price - prev_price)
    change_pct: float    # percentage change (e.g. 1.23 = 1.23%)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat' — drives frontend flash animation color."""
        if self.change > 0:
            return "up"
        elif self.change < 0:
            return "down"
        return "flat"


@dataclass
class DailyBar:
    ticker: str
    date: str            # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None = None
```

**Design notes:**
- `change_pct` is intraday vs. `prev_price` (the previous tick in the simulator, or the previous day's close from Massive). This drives the "daily change %" column in the watchlist panel.
- `direction` is a derived property — not stored, computed on access. The SSE serializer calls it once per update.
- `DailyBar` is returned by `get_daily_bars()`. The simulator always returns `[]` (no historical data); MassiveClient fetches from the aggregates endpoint.

---

## 2. Price Cache

**File:** `backend/app/market/cache.py`

The cache is the single shared mutable state that ties the data source to SSE streaming and API reads.

```python
import asyncio
from .models import PriceUpdate


class PriceCache:
    """In-memory store for the latest price of every tracked ticker.

    Write path: background market data task calls update() after each poll cycle.
    Read path: SSE endpoint and API routes call get() / get_all() — plain dict lookup.
    Push path: update() puts each PriceUpdate into every subscriber's asyncio.Queue.

    CPython's GIL makes individual dict reads atomic, so reads don't need locking.
    Writes use a lock to prevent torn state during bulk updates (multiple tickers
    updated in one call must appear as a consistent snapshot).
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = asyncio.Lock()
        self._subscribers: list[asyncio.Queue] = []

    async def update(self, updates: list[PriceUpdate]) -> None:
        """Write new prices and notify all SSE subscribers."""
        async with self._lock:
            for u in updates:
                self._prices[u.ticker] = u
        for queue in self._subscribers:
            for u in updates:
                try:
                    queue.put_nowait(u)
                except asyncio.QueueFull:
                    pass  # slow client — drop the update; client will catch up

    def get(self, ticker: str) -> PriceUpdate | None:
        return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        return dict(self._prices)  # shallow copy — safe for iteration

    def get_tickers(self) -> list[str]:
        return list(self._prices.keys())

    def subscribe(self) -> asyncio.Queue:
        """Return a new queue that receives every PriceUpdate going forward.

        Each connected SSE client gets its own queue. The background task puts
        into all queues on every update cycle — fan-out without contention.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove queue when an SSE client disconnects."""
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass
```

**Key design decisions:**

- `put_nowait` with `QueueFull` swallow: slow SSE clients can't block the update path. A client that falls 1000 updates behind will miss some ticks, which is acceptable for a display-only stream.
- The write lock protects the bulk update (multiple tickers per poll) from being partially visible to readers mid-write. Reads do not lock — CPython's GIL makes `dict.__getitem__` atomic.
- `subscribe()`/`unsubscribe()` are called from the FastAPI SSE endpoint on client connect/disconnect.

---

## 3. Abstract Interface

**File:** `backend/app/market/interface.py`

```python
from abc import ABC, abstractmethod
from .models import PriceUpdate, DailyBar


class MarketDataSource(ABC):
    """Contract that both MarketSimulator and MassiveClient must satisfy.

    All code outside backend/app/market/ must never import a concrete
    implementation directly — only use this interface (or the factory).
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Start the background loop. Called once at app startup."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Cancel the background task. Called on app shutdown."""
        ...

    @abstractmethod
    def add_ticker(self, ticker: str) -> None:
        """Register a new ticker; it will be included in the next poll/tick."""
        ...

    @abstractmethod
    def remove_ticker(self, ticker: str) -> None:
        """Deregister a ticker. Its cached price remains until overwritten."""
        ...

    @abstractmethod
    async def get_daily_bars(
        self, ticker: str, from_date: str, to_date: str
    ) -> list[DailyBar]:
        """Fetch historical daily OHLCV bars, sorted ascending by date.

        Returns [] for sources that don't support historical data (simulator).
        """
        ...
```

---

## 4. Market Simulator

**File:** `backend/app/market/simulator.py`

The simulator uses **Geometric Brownian Motion (GBM)** with correlated random draws across tickers.

### Mathematical model

Each ticker price evolves as:

```
S(t + Δt) = S(t) · exp((μ - σ²/2)·Δt + σ·√Δt · Z)
```

- `μ` — drift per second (annualised drift / seconds_per_year)
- `σ` — volatility per second (annualised vol / √seconds_per_year)
- `Δt = 0.5` seconds per tick
- `Z ~ N(0,1)` — drawn as a correlated multivariate normal using Cholesky decomposition

**Why GBM?** Prices stay positive (exponential), returns are log-normally distributed (matching real equity data), and the math is straightforward to implement with numpy.

### Correlated returns

Real stocks don't move independently — tech stocks co-move. We model this with Cholesky decomposition:

```python
# L = Cholesky factor of correlation matrix C, so L @ L.T = C
L = np.linalg.cholesky(C)

z_independent = np.random.standard_normal(n)  # one per ticker
z_correlated = L @ z_independent              # now correlated
```

Each element of `z_correlated` becomes the `Z` for its ticker's GBM step.

### Full implementation

```python
import asyncio
import logging
import numpy as np
from datetime import datetime, timezone
from typing import NamedTuple

from .interface import MarketDataSource
from .cache import PriceCache
from .models import PriceUpdate, DailyBar

logger = logging.getLogger(__name__)

SECONDS_PER_YEAR = 252 * 6.5 * 3600  # ~5.9M trading seconds/year
TICK_INTERVAL = 0.5                   # seconds between price updates

# (seed_price, annual_drift, annual_volatility)
TICKER_PARAMS: dict[str, tuple[float, float, float]] = {
    "AAPL":  (190.00, 0.15, 0.25),
    "GOOGL": (175.00, 0.12, 0.28),
    "MSFT":  (415.00, 0.18, 0.22),
    "AMZN":  (185.00, 0.20, 0.30),
    "TSLA":  (250.00, 0.08, 0.55),   # high vol
    "NVDA":  (875.00, 0.35, 0.50),   # high growth + vol
    "META":  (520.00, 0.25, 0.32),
    "JPM":   (200.00, 0.10, 0.20),   # lower vol (financials)
    "V":     (270.00, 0.12, 0.18),   # lower vol (payments)
    "NFLX":  (700.00, 0.15, 0.38),
}

#         AAPL   GOOGL  MSFT   AMZN   TSLA   NVDA   META   JPM    V      NFLX
CORRELATION_MATRIX = np.array([
    [1.00,  0.65,  0.70,  0.55,  0.45,  0.60,  0.60,  0.30,  0.35,  0.50],
    [0.65,  1.00,  0.65,  0.60,  0.40,  0.55,  0.65,  0.25,  0.30,  0.55],
    [0.70,  0.65,  1.00,  0.55,  0.42,  0.60,  0.58,  0.30,  0.32,  0.48],
    [0.55,  0.60,  0.55,  1.00,  0.40,  0.50,  0.60,  0.25,  0.35,  0.60],
    [0.45,  0.40,  0.42,  0.40,  1.00,  0.55,  0.38,  0.20,  0.22,  0.40],
    [0.60,  0.55,  0.60,  0.50,  0.55,  1.00,  0.52,  0.25,  0.28,  0.45],
    [0.60,  0.65,  0.58,  0.60,  0.38,  0.52,  1.00,  0.25,  0.30,  0.58],
    [0.30,  0.25,  0.30,  0.25,  0.20,  0.25,  0.25,  1.00,  0.65,  0.22],
    [0.35,  0.30,  0.32,  0.35,  0.22,  0.28,  0.30,  0.65,  1.00,  0.28],
    [0.50,  0.55,  0.48,  0.60,  0.40,  0.45,  0.58,  0.22,  0.28,  1.00],
])
# Row/column order matches list(TICKER_PARAMS.keys())

P_EVENT = 0.002          # ~0.2% chance of shock per ticker per tick (~1/4 min)
EVENT_REVERT_TICKS = 10  # suppress drift for 5 seconds after a shock


class _TickerState(NamedTuple):
    price: float
    drift: float   # per-tick drift, pre-computed at start
    vol: float     # per-tick volatility, pre-computed at start
    revert: int    # ticks remaining in post-shock mean-reversion window


class MarketSimulator(MarketDataSource):
    """Synthetic price engine using correlated GBM.

    Default tickers use a shared 10x10 correlation matrix.
    Unknown tickers added at runtime are assigned a default correlation of 0.3
    with all others and the generic parameters (seed=100, drift=12%, vol=30%).
    """

    def __init__(self, cache: PriceCache) -> None:
        self._cache = cache
        self._tickers: list[str] = []
        self._states: dict[str, _TickerState] = {}
        self._cholesky: np.ndarray | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._init_tickers(tickers)
        self._task = asyncio.create_task(self._tick_loop())
        logger.info("MarketSimulator started with %d tickers", len(self._tickers))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MarketSimulator stopped")

    def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper()
        if ticker not in self._states:
            self._states[ticker] = self._make_state(ticker)
            self._tickers.append(ticker)
            self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper()
        if ticker in self._states:
            del self._states[ticker]
            self._tickers = [t for t in self._tickers if t != ticker]
            self._rebuild_cholesky()

    async def get_daily_bars(self, ticker: str, from_date: str, to_date: str) -> list[DailyBar]:
        return []  # no historical data in simulator

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _make_state(self, ticker: str) -> _TickerState:
        seed_price, annual_drift, annual_vol = TICKER_PARAMS.get(
            ticker, (100.0, 0.12, 0.30)
        )
        drift_per_tick = (annual_drift - 0.5 * annual_vol ** 2) * (TICK_INTERVAL / SECONDS_PER_YEAR)
        vol_per_tick = annual_vol * (TICK_INTERVAL / SECONDS_PER_YEAR) ** 0.5
        return _TickerState(price=seed_price, drift=drift_per_tick, vol=vol_per_tick, revert=0)

    def _init_tickers(self, tickers: list[str]) -> None:
        self._tickers = [t.upper() for t in tickers]
        for ticker in self._tickers:
            self._states[ticker] = self._make_state(ticker)
        self._rebuild_cholesky()

    def _rebuild_cholesky(self) -> None:
        """Recompute the Cholesky factor whenever the ticker list changes."""
        n = len(self._tickers)
        if n == 0:
            self._cholesky = None
            return

        default_order = list(TICKER_PARAMS.keys())
        default_idx = {t: i for i, t in enumerate(default_order)}

        C = np.eye(n)
        for i, ti in enumerate(self._tickers):
            for j, tj in enumerate(self._tickers):
                if i == j:
                    continue
                if ti in default_idx and tj in default_idx:
                    C[i, j] = CORRELATION_MATRIX[default_idx[ti], default_idx[tj]]
                else:
                    C[i, j] = 0.3  # default cross-asset correlation for unknown tickers

        self._cholesky = np.linalg.cholesky(C)

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_INTERVAL)
            updates = self._compute_tick()
            if updates:
                await self._cache.update(updates)

    def _compute_tick(self) -> list[PriceUpdate]:
        n = len(self._tickers)
        if n == 0 or self._cholesky is None:
            return []

        # Draw correlated normals for this tick
        z = self._cholesky @ np.random.standard_normal(n)

        now = datetime.now(tz=timezone.utc)
        updates: list[PriceUpdate] = []
        new_states: dict[str, _TickerState] = {}

        for i, ticker in enumerate(self._tickers):
            state = self._states[ticker]

            # Shock event: override GBM with a sudden ±5% jump
            if np.random.random() < P_EVENT:
                shock_pct = np.random.uniform(-0.05, 0.05)
                new_price = max(state.price * (1 + shock_pct), 0.01)
                change = new_price - state.price
                updates.append(PriceUpdate(
                    ticker=ticker,
                    price=round(new_price, 4),
                    prev_price=round(state.price, 4),
                    timestamp=now,
                    change=round(change, 4),
                    change_pct=round(shock_pct * 100, 4),
                ))
                new_states[ticker] = _TickerState(
                    price=new_price, drift=state.drift,
                    vol=state.vol, revert=EVENT_REVERT_TICKS,
                )
                continue

            # Normal GBM step
            effective_drift = 0.0 if state.revert > 0 else state.drift
            log_return = effective_drift + state.vol * z[i]
            new_price = max(state.price * np.exp(log_return), 0.01)
            change = new_price - state.price
            change_pct = (change / state.price * 100) if state.price else 0.0

            updates.append(PriceUpdate(
                ticker=ticker,
                price=round(new_price, 4),
                prev_price=round(state.price, 4),
                timestamp=now,
                change=round(change, 4),
                change_pct=round(change_pct, 4),
            ))
            new_states[ticker] = _TickerState(
                price=new_price, drift=state.drift,
                vol=state.vol, revert=max(0, state.revert - 1),
            )

        for ticker, state in new_states.items():
            self._states[ticker] = state

        return updates
```

### Simulator behavior summary

| Property | Value |
|---|---|
| Update frequency | Every 500ms |
| Typical move per tick | 0.01–0.05% |
| Shock probability | ~0.2% per ticker per tick (~once per 4 min) |
| Shock magnitude | ±0–5% instantaneous |
| Post-shock drift suppression | 5 seconds (10 ticks) |
| Intraday range (typical) | 1–3% for low-vol, 3–8% for high-vol |
| Historical data | None (`get_daily_bars` returns `[]`) |

---

## 5. Massive API Client

**File:** `backend/app/market/massive_client.py`

Polls the Massive (formerly Polygon.io) REST snapshot endpoint on a configurable interval. All I/O is async via `httpx`.

### Why httpx, not the official `massive` Python library?

The official `RESTClient` is synchronous. In an async FastAPI app, calling it directly blocks the event loop. Using `httpx.AsyncClient` keeps everything non-blocking.

### Implementation

```python
import asyncio
import logging
import httpx
from datetime import datetime, timezone

from .interface import MarketDataSource
from .cache import PriceCache
from .models import PriceUpdate, DailyBar

logger = logging.getLogger(__name__)

BASE_URL = "https://api.massive.com"


class MassiveClient(MarketDataSource):
    """Polls the Massive REST snapshot endpoint and writes to PriceCache.

    Poll interval guide:
      - Free tier (5 req/min):   poll_interval=15.0s (safe)
      - Starter+ (unlimited):    poll_interval=2.0–5.0s
    """

    def __init__(self, api_key: str, cache: PriceCache, poll_interval: float = 15.0) -> None:
        self._api_key = api_key
        self._cache = cache
        self._poll_interval = poll_interval
        self._tickers: set[str] = set()
        self._task: asyncio.Task | None = None
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def start(self, tickers: list[str]) -> None:
        self._tickers = {t.upper() for t in tickers}
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("MassiveClient started, polling every %.1fs", self._poll_interval)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MassiveClient stopped")

    def add_ticker(self, ticker: str) -> None:
        self._tickers.add(ticker.upper())

    def remove_ticker(self, ticker: str) -> None:
        self._tickers.discard(ticker.upper())

    async def _poll_loop(self) -> None:
        async with httpx.AsyncClient(timeout=10.0) as http:
            while True:
                if self._tickers:
                    updates = await self._fetch_snapshots(http)
                    if updates:
                        await self._cache.update(updates)
                await asyncio.sleep(self._poll_interval)

    async def _fetch_snapshots(self, http: httpx.AsyncClient) -> list[PriceUpdate]:
        url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers"
        params = {"tickers": ",".join(sorted(self._tickers))}
        try:
            response = await http.get(url, params=params, headers=self._headers)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 403:
                logger.error("Massive API key invalid or missing (403). Stopping polls.")
                self._tickers.clear()  # prevent hammering with a bad key
            elif status == 429:
                logger.warning("Massive API rate limit hit (429). Backing off 60s.")
                await asyncio.sleep(60)
            else:
                logger.error("Massive API HTTP %d. Retrying after 5s.", status)
                await asyncio.sleep(5)
            return []
        except httpx.RequestError as e:
            logger.error("Network error polling Massive: %s. Retrying after 5s.", e)
            await asyncio.sleep(5)
            return []

        if data.get("status") != "OK":
            logger.warning("Unexpected Massive API status: %s", data.get("status"))
            return []

        return self._parse_snapshots(data)

    def _parse_snapshots(self, data: dict) -> list[PriceUpdate]:
        updates = []
        for t in data.get("tickers", []):
            try:
                ticker = t["ticker"]
                # Prefer lastTrade.p (real-time, Advanced plan); fall back to day.c
                last_trade = t.get("lastTrade") or {}
                price = last_trade.get("p") or t["day"]["c"]
                prev_close = t["prevDay"]["c"]
                # Use Massive's pre-computed daily change fields for accuracy
                change = t.get("todaysChange", price - prev_close)
                change_pct = t.get("todaysChangePerc", (change / prev_close * 100) if prev_close else 0.0)
                ts_ns = t.get("updated", 0)
                timestamp = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc) if ts_ns else datetime.now(tz=timezone.utc)
                updates.append(PriceUpdate(
                    ticker=ticker,
                    price=round(price, 4),
                    prev_price=round(prev_close, 4),
                    timestamp=timestamp,
                    change=round(change, 4),
                    change_pct=round(change_pct, 4),
                ))
            except (KeyError, TypeError, ZeroDivisionError) as e:
                logger.warning("Skipping malformed snapshot for %s: %s", t.get("ticker"), e)
        return updates

    async def get_daily_bars(self, ticker: str, from_date: str, to_date: str) -> list[DailyBar]:
        url = f"{BASE_URL}/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}"
        params = {"adjusted": "true", "sort": "asc", "limit": 5000}
        async with httpx.AsyncClient(timeout=15.0) as http:
            try:
                response = await http.get(url, params=params, headers=self._headers)
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, Exception) as e:
                logger.error("Failed to fetch daily bars for %s: %s", ticker, e)
                return []

        bars = []
        for r in data.get("results", []):
            dt = datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc)
            bars.append(DailyBar(
                ticker=ticker,
                date=dt.strftime("%Y-%m-%d"),
                open=r["o"],
                high=r["h"],
                low=r["l"],
                close=r["c"],
                volume=int(r["v"]),
                vwap=r.get("vw"),
            ))
        return bars
```

### Massive API field mapping

| `PriceUpdate` field | Massive field | Notes |
|---|---|---|
| `price` | `lastTrade.p` or `day.c` | `lastTrade.p` requires Advanced plan |
| `prev_price` | `prevDay.c` | Previous session's close |
| `change` | `todaysChange` | Dollar change vs. prev close |
| `change_pct` | `todaysChangePerc` | % change vs. prev close |
| `timestamp` | `updated` (nanoseconds) | Divided by 1e9 for seconds |

---

## 6. Factory

**File:** `backend/app/market/factory.py`

```python
import os
import logging
from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(cache: PriceCache) -> MarketDataSource:
    """Instantiate the correct MarketDataSource based on environment config.

    Selection logic:
      - MASSIVE_API_KEY set and non-empty  →  MassiveClient
      - Otherwise                           →  MarketSimulator

    Optional env vars:
      - MASSIVE_POLL_INTERVAL  float seconds, default 15.0 (free tier safe)
    """
    api_key = os.getenv("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveClient
        poll_interval = float(os.getenv("MASSIVE_POLL_INTERVAL", "15.0"))
        logger.info("Market data: Massive API (poll_interval=%.1fs)", poll_interval)
        return MassiveClient(api_key=api_key, cache=cache, poll_interval=poll_interval)

    from .simulator import MarketSimulator
    logger.info("Market data: Simulator (no MASSIVE_API_KEY set)")
    return MarketSimulator(cache=cache)
```

---

## 7. Public Package API

**File:** `backend/app/market/__init__.py`

```python
from .cache import PriceCache
from .models import PriceUpdate, DailyBar
from .interface import MarketDataSource
from .factory import create_market_data_source

__all__ = [
    "PriceCache",
    "PriceUpdate",
    "DailyBar",
    "MarketDataSource",
    "create_market_data_source",
]
```

Concrete implementations (`MarketSimulator`, `MassiveClient`) are intentionally not exported. All external code receives a `MarketDataSource`.

---

## 8. FastAPI Integration

**File:** `backend/app/main.py` (relevant excerpts)

### App startup/shutdown

```python
import asyncio
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from .market import PriceCache, create_market_data_source
from .database import get_watchlist_tickers  # returns list[str] from DB

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                   "NVDA", "META", "JPM", "V", "NFLX"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = PriceCache()
    source = create_market_data_source(cache)

    # Load watchlist from DB; fall back to defaults on a fresh install
    tickers = await get_watchlist_tickers() or DEFAULT_TICKERS
    await source.start(tickers)

    app.state.cache = cache
    app.state.market = source
    yield
    await source.stop()


app = FastAPI(lifespan=lifespan)
```

### SSE streaming endpoint

```python
@app.get("/api/stream/prices")
async def stream_prices(request: Request):
    cache: PriceCache = request.app.state.cache
    queue = cache.subscribe()

    async def event_generator():
        try:
            # Snapshot: push all currently-known prices immediately on connect
            # so the watchlist isn't empty while waiting for the next tick
            for update in cache.get_all().values():
                payload = _serialize_update(update)
                yield f"data: {json.dumps(payload)}\n\n"

            # Stream: push new updates as they arrive
            while True:
                if await request.is_disconnected():
                    break
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {json.dumps(_serialize_update(update))}\n\n"
                except asyncio.TimeoutError:
                    # Keepalive comment — prevents proxy timeouts and lets the
                    # client detect a broken connection sooner than TCP timeout
                    yield ": keepalive\n\n"
        finally:
            cache.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


def _serialize_update(update) -> dict:
    return {
        "ticker": update.ticker,
        "price": update.price,
        "prev_price": update.prev_price,
        "change": update.change,
        "change_pct": update.change_pct,
        "direction": update.direction,
        "timestamp": update.timestamp.isoformat(),
    }
```

**SSE design notes:**
- The keepalive fires every 25 seconds of silence. This prevents load balancers and proxies (nginx, AWS ALB) from closing idle connections.
- `X-Accel-Buffering: no` is critical when running behind nginx — without it, nginx buffers SSE until the buffer fills, breaking the live-update effect.
- The initial snapshot is sent before the streaming loop, so a newly connected client sees current prices without waiting for the next tick.

### Watchlist endpoints (updating the live source)

```python
from pydantic import BaseModel

class AddTickerRequest(BaseModel):
    ticker: str


@app.post("/api/watchlist")
async def add_to_watchlist(body: AddTickerRequest, request: Request):
    ticker = body.ticker.upper().strip()
    # Validate ticker exists (optional — can rely on market source to handle gracefully)
    await db_add_to_watchlist(ticker)              # persist to SQLite
    request.app.state.market.add_ticker(ticker)    # register with market source
    return {"ticker": ticker, "added": True}


@app.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(ticker: str, request: Request):
    ticker = ticker.upper()
    await db_remove_from_watchlist(ticker)          # remove from SQLite
    request.app.state.market.remove_ticker(ticker)  # deregister from market source
    # Note: price remains in cache until overwritten; positions still get live prices
    return {"ticker": ticker, "removed": True}
```

**Important:** when a user removes a ticker from the watchlist, `remove_ticker()` only stops updating that ticker in the cache — it does **not** delete the cached price. This is intentional: if the user holds a position in the removed ticker, the portfolio P&L route still needs its last-known price.

---

## 9. Price Coverage: Watchlist vs. Positions

The PLAN.md raises an open question: *"Should the price cache cover `positions ∪ watchlist`, not just the watchlist?"*

**Answer: yes.** The portfolio P&L calculation requires a live price for every held ticker, regardless of whether it's on the watchlist. The backend should ensure all tickers in active positions are registered with the market source.

On startup, add this to the lifespan:

```python
from .database import get_position_tickers  # returns list[str]

position_tickers = await get_position_tickers()
all_tickers = list(set(tickers) | set(position_tickers))
await source.start(all_tickers)
```

And in the trade execution handler, register the ticker when a position is opened:

```python
@app.post("/api/portfolio/trade")
async def execute_trade(body: TradeRequest, request: Request):
    # ... validation, DB write ...
    request.app.state.market.add_ticker(body.ticker)  # ensure live price tracking
    # ...
```

---

## 10. Testing

### Unit test: simulator produces valid prices

```python
import asyncio
import pytest
from backend.app.market.cache import PriceCache
from backend.app.market.simulator import MarketSimulator


@pytest.mark.asyncio
async def test_simulator_produces_positive_prices():
    cache = PriceCache()
    sim = MarketSimulator(cache)
    await sim.start(["AAPL", "TSLA"])
    await asyncio.sleep(1.5)  # 3 ticks at 500ms
    await sim.stop()

    prices = cache.get_all()
    assert "AAPL" in prices
    assert "TSLA" in prices
    assert prices["AAPL"].price > 0
    assert prices["TSLA"].price > 0
    assert prices["AAPL"].direction in ("up", "down", "flat")


@pytest.mark.asyncio
async def test_simulator_add_remove_ticker():
    cache = PriceCache()
    sim = MarketSimulator(cache)
    await sim.start(["AAPL"])
    sim.add_ticker("NVDA")
    await asyncio.sleep(1.0)
    sim.remove_ticker("NVDA")
    await asyncio.sleep(0.5)
    await sim.stop()

    # NVDA was populated before removal; its cache entry remains
    assert cache.get("NVDA") is not None
```

### Unit test: cache fan-out

```python
@pytest.mark.asyncio
async def test_cache_subscriber_receives_updates():
    cache = PriceCache()
    q = cache.subscribe()

    from datetime import datetime, timezone
    from backend.app.market.models import PriceUpdate

    update = PriceUpdate("AAPL", 190.0, 189.5, datetime.now(timezone.utc), 0.5, 0.26)
    await cache.update([update])

    received = await asyncio.wait_for(q.get(), timeout=1.0)
    assert received.ticker == "AAPL"
    assert received.price == 190.0

    cache.unsubscribe(q)
    assert q not in cache._subscribers
```

### Unit test: MassiveClient snapshot parsing

```python
from backend.app.market.massive_client import MassiveClient
from backend.app.market.cache import PriceCache


def test_parse_snapshots_happy_path():
    cache = PriceCache()
    client = MassiveClient(api_key="test", cache=cache)

    data = {
        "status": "OK",
        "tickers": [{
            "ticker": "AAPL",
            "todaysChange": 1.23,
            "todaysChangePerc": 0.65,
            "updated": 1700000000_000_000_000,  # nanoseconds
            "day": {"o": 189.0, "h": 191.0, "l": 188.0, "c": 190.5, "v": 50000000, "vw": 190.1},
            "prevDay": {"o": 188.0, "h": 190.0, "l": 187.0, "c": 189.27, "v": 48000000, "vw": 188.9},
            "lastTrade": {"p": 190.54, "s": 100, "t": 1700000000_000_000_000},
        }]
    }

    updates = client._parse_snapshots(data)
    assert len(updates) == 1
    u = updates[0]
    assert u.ticker == "AAPL"
    assert u.price == 190.54        # from lastTrade.p
    assert u.prev_price == 189.27   # from prevDay.c
    assert u.change == 1.23         # from todaysChange
    assert u.change_pct == 0.65     # from todaysChangePerc
    assert u.direction == "up"


def test_parse_snapshots_missing_last_trade_falls_back_to_day_close():
    cache = PriceCache()
    client = MassiveClient(api_key="test", cache=cache)

    data = {
        "status": "OK",
        "tickers": [{
            "ticker": "MSFT",
            "todaysChange": -2.10,
            "todaysChangePerc": -0.50,
            "updated": 0,
            "day": {"c": 413.5, "o": 415.0, "h": 416.0, "l": 412.0, "v": 20000000},
            "prevDay": {"c": 415.6},
            # no lastTrade field
        }]
    }
    updates = client._parse_snapshots(data)
    assert updates[0].price == 413.5   # fell back to day.c
```

### Integration test: factory selects correct source

```python
import os
from unittest.mock import patch
from backend.app.market.cache import PriceCache
from backend.app.market.factory import create_market_data_source
from backend.app.market.simulator import MarketSimulator
from backend.app.market.massive_client import MassiveClient


def test_factory_returns_simulator_when_no_key():
    cache = PriceCache()
    with patch.dict(os.environ, {}, clear=True):
        source = create_market_data_source(cache)
    assert isinstance(source, MarketSimulator)


def test_factory_returns_massive_client_when_key_set():
    cache = PriceCache()
    with patch.dict(os.environ, {"MASSIVE_API_KEY": "test-key-123"}):
        source = create_market_data_source(cache)
    assert isinstance(source, MassiveClient)
```

---

## 11. Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `MASSIVE_API_KEY` | *(unset)* | If set, enables the Massive API client |
| `MASSIVE_POLL_INTERVAL` | `15.0` | Seconds between polls (float). Set lower on paid tiers. |

---

## 12. Known Limitations and Open Questions

### Simulator

- **No daily open**: the "daily change %" shown in the watchlist is intraday change since the previous tick (simulator) vs. previous session's close (Massive). These are semantically different. The frontend should label this column "Change" or "Change from last", not "Daily Change %", to avoid misrepresenting the simulator data.
- **No historical data**: `get_daily_bars()` returns `[]`. The main chart area will only show data accumulated since page load via SSE. A `GET /api/prices/{ticker}/history` endpoint backed by the SSE event buffer (stored in the DB as a new `price_history` table) would solve this but is not in the current plan.
- **24/7 updates**: the simulator runs continuously regardless of market hours. Acceptable for a demo.

### Massive API

- **Model name discrepancy** (from PLAN.md §9): the model `openrouter/openai/gpt-oss-120b` does not correspond to a known model. The intended Cerebras-hosted model on OpenRouter is likely `openrouter/cerebras/llama-3.3-70b`. Confirm before implementing the LLM chat feature.
- **Free tier delay**: free-tier Massive data is 15 minutes delayed. This is real data but not real-time. Users should be informed of this via a UI indicator if `MASSIVE_API_KEY` is set without a paid plan.
- **No watchlist-change detection**: both `add_ticker`/`remove_ticker` are synchronous calls that mutate the in-flight `_tickers` set. The next poll cycle picks up the change automatically. There's a window of up to one full poll interval during which a newly-added ticker has no cached price — `cache.get(ticker)` returns `None`. The watchlist API response for a just-added ticker should return `null` for price and the frontend should handle this gracefully (show "—" rather than $0.00).
