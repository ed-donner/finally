# Market Data Backend — Complete Implementation Design

This document is the single implementation reference for all market data functionality in FinAlly. It synthesises `MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, and `MASSIVE_API.md` into a complete, copy-paste-ready guide for the Backend Engineer agent.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Module Structure](#2-module-structure)
3. [Resolved Design Decisions](#3-resolved-design-decisions)
4. [Data Models](#4-data-models)
5. [Price Cache](#5-price-cache)
6. [Abstract Interface](#6-abstract-interface)
7. [Market Simulator](#7-market-simulator)
8. [Massive API Client](#8-massive-api-client)
9. [Factory Function](#9-factory-function)
10. [FastAPI Integration](#10-fastapi-integration)
11. [SSE Streaming Endpoint](#11-sse-streaming-endpoint)
12. [Watchlist API (market layer)](#12-watchlist-api-market-layer)
13. [Testing](#13-testing)

---

## 1. Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│  FastAPI process                                             │
│                                                              │
│  ┌─────────────────────────┐    ┌────────────────────────┐  │
│  │  MarketDataSource       │    │  PriceCache             │  │
│  │  (one of:)              │───▶│  (shared in-memory)     │  │
│  │  • MarketSimulator      │    │                         │  │
│  │  • MassiveClient        │    │  • prices dict          │  │
│  └─────────────────────────┘    │  • subscriber queues    │  │
│       background asyncio task   └────────┬───────────────┘  │
│                                          │                   │
│                           ┌──────────────┴──────────┐        │
│                           │  SSE /api/stream/prices │        │
│                           │  per-client Queue fan-out│       │
│                           └─────────────────────────┘        │
│                                          │                   │
│                           ┌──────────────▼──────────┐        │
│                           │  API routes              │        │
│                           │  /api/watchlist          │        │
│                           │  /api/portfolio          │        │
│                           └─────────────────────────┘        │
└──────────────────────────────────────────────────────────────┘
```

**Data flow:**
1. A single background `asyncio` task (simulator or Massive poller) computes new prices every tick.
2. It calls `cache.update(updates)`, which atomically updates the dict and fans out to every connected SSE subscriber queue.
3. SSE endpoint drains its queue and formats each update as an SSE event.
4. All other code (portfolio P&L, watchlist API) reads synchronously from the cache dict—no async needed.

**Ticker scope:** The cache tracks `positions ∪ watchlist`. When the user holds a position in a ticker they have removed from the watchlist, the market source continues tracking it so portfolio P&L can be computed. See §12 for implementation details.

---

## 2. Module Structure

```
backend/app/market/
├── __init__.py          # public re-exports
├── models.py            # PriceUpdate, DailyBar
├── cache.py             # PriceCache
├── interface.py         # MarketDataSource ABC
├── simulator.py         # MarketSimulator (GBM)
├── massive_client.py    # MassiveClient (REST polling)
└── factory.py           # create_market_data_source()
```

`__init__.py` content:

```python
from .models import PriceUpdate, DailyBar
from .cache import PriceCache
from .interface import MarketDataSource
from .factory import create_market_data_source

__all__ = [
    "PriceUpdate",
    "DailyBar",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
]
```

---

## 3. Resolved Design Decisions

These answer the open questions raised in `PLAN.md §13`.

### "Daily change %" definition

- **Massive API:** Use `todaysChangePerc` from the snapshot response. This is vs. the previous trading day's close — the standard definition.
- **Simulator:** No concept of a "previous close." Use **change since simulator start** (i.e., `(current_price - seed_price) / seed_price * 100`). The `PriceUpdate` model's `change_pct` field carries this value. Frontend should label it "since open" for the simulator and "daily %" for real data; the backend returns the same field in both cases.

### Ticker scope for price cache

The cache tracks `watchlist ∪ positions`. The `start()` method receives this union from the database. When a position exists in a ticker removed from the watchlist, `remove_ticker()` should **not** remove it from the source if a position still exists. The watchlist API route is responsible for this check before calling `remove_ticker()`. See §12.

### P&L chart empty on first load

The background portfolio snapshot task (not part of this module) should record an initial snapshot immediately on startup, before waiting 30 seconds. This is a backend concern outside the market module but is noted here as the constraint the market module must satisfy: **prices must be in the cache before the first portfolio snapshot is taken**. The `start()` method pushes an initial `cache.update()` for all tickers immediately before launching the loop, so the cache is non-empty by the time the FastAPI lifespan yields.

### SSE loop vs. simulator loop — same timer?

They are **separate**. The simulator runs its own internal `asyncio.sleep(0.5)` loop. The SSE endpoint does **not** have its own timer — it simply awaits the next item from its subscriber queue. This means the SSE push rate is exactly equal to the simulator tick rate (or Massive poll rate), with no duplicate pushes for unchanged prices.

### Conversation history limit (LLM, out of scope here)

Not addressed in this document. See the LLM integration document.

---

## 4. Data Models

`backend/app/market/models.py`

```python
from dataclasses import dataclass
from datetime import datetime


@dataclass
class PriceUpdate:
    ticker: str
    price: float
    prev_price: float
    timestamp: datetime
    change: float        # absolute dollar change from prev_price
    change_pct: float    # percentage change (e.g. 1.23 means +1.23%)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat' — drives frontend flash animation colour."""
        if self.change > 0:
            return "up"
        if self.change < 0:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "price": self.price,
            "prev_price": self.prev_price,
            "change": self.change,
            "change_pct": self.change_pct,
            "direction": self.direction,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class DailyBar:
    ticker: str
    date: str        # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None = None
```

---

## 5. Price Cache

`backend/app/market/cache.py`

```python
import asyncio
from .models import PriceUpdate


class PriceCache:
    """Shared in-memory store for the latest price of every tracked ticker.

    The background market data task is the sole writer; the SSE endpoint and
    all API routes are readers. Reads are plain dict lookups (no lock) because
    CPython's GIL makes per-item dict reads atomic. Writes take an asyncio.Lock
    to prevent torn state during bulk updates.

    SSE fan-out: each connected client calls subscribe() to get a private Queue.
    Every cache.update() puts each PriceUpdate onto all subscriber queues.
    When the SSE connection closes, the route calls unsubscribe().
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = asyncio.Lock()
        self._subscribers: list[asyncio.Queue[PriceUpdate]] = []

    # ------------------------------------------------------------------
    # Write path (background task only)
    # ------------------------------------------------------------------

    async def update(self, updates: list[PriceUpdate]) -> None:
        async with self._lock:
            for u in updates:
                self._prices[u.ticker] = u
        # Fan out to all SSE subscribers outside the lock
        for queue in self._subscribers:
            for u in updates:
                try:
                    queue.put_nowait(u)
                except asyncio.QueueFull:
                    # Slow client — drop oldest item and insert new one
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    queue.put_nowait(u)

    # ------------------------------------------------------------------
    # Read path (API routes, portfolio calculations)
    # ------------------------------------------------------------------

    def get(self, ticker: str) -> PriceUpdate | None:
        return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        return dict(self._prices)  # shallow copy so callers can iterate safely

    def get_tickers(self) -> list[str]:
        return list(self._prices.keys())

    def current_price(self, ticker: str) -> float | None:
        u = self._prices.get(ticker)
        return u.price if u else None

    # ------------------------------------------------------------------
    # SSE subscription management
    # ------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[PriceUpdate]:
        queue: asyncio.Queue[PriceUpdate] = asyncio.Queue(maxsize=500)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[PriceUpdate]) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass
```

---

## 6. Abstract Interface

`backend/app/market/interface.py`

```python
from abc import ABC, abstractmethod
from .models import PriceUpdate, DailyBar


class MarketDataSource(ABC):
    """Abstract base for MarketSimulator and MassiveClient.

    All code outside backend/app/market/ must interact with prices only
    through PriceCache, not by calling methods on the source directly.
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin the background update loop for the given tickers.

        Implementations must push an initial snapshot to the cache before
        the loop begins, so the cache is non-empty when the app starts serving.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Cancel the background task and clean up resources."""
        ...

    @abstractmethod
    def add_ticker(self, ticker: str) -> None:
        """Register a new ticker to track on the next cycle.

        Called when a ticker is added to the watchlist or a position is opened.
        Must be safe to call at any time from within a request handler.
        """
        ...

    @abstractmethod
    def remove_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker.

        Called only when the ticker is absent from both the watchlist and
        all open positions. The caller (watchlist route) is responsible for
        this check.
        """
        ...

    @abstractmethod
    async def get_daily_bars(
        self, ticker: str, from_date: str, to_date: str
    ) -> list[DailyBar]:
        """Return historical daily OHLCV bars sorted ascending by date.

        Returns an empty list if the source does not support historical data
        (e.g. the simulator).
        """
        ...
```

---

## 7. Market Simulator

`backend/app/market/simulator.py`

### Mathematical Model

Each ticker price evolves each tick via discrete Geometric Brownian Motion:

```
S(t + Δt) = S(t) · exp((μ - σ²/2)·Δt + σ·√Δt · Z)
```

- `μ` — annualised drift, converted to per-tick: `drift_per_tick = (μ - σ²/2) * (Δt / SECONDS_PER_YEAR)`
- `σ` — annualised volatility, converted to per-tick: `vol_per_tick = σ * √(Δt / SECONDS_PER_YEAR)`
- `Δt = 0.5` seconds
- `Z` — correlated standard normal (from Cholesky decomposition)

Correlated normals across tickers are produced by:

```python
z_uncorrelated = np.random.standard_normal(n_tickers)
z_correlated = cholesky_factor @ z_uncorrelated
```

### Full Implementation

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

SECONDS_PER_YEAR = 252 * 6.5 * 3600   # ≈ 5,901,120
TICK_INTERVAL = 0.5                    # seconds between price updates

# (seed_price, annual_drift, annual_volatility)
TICKER_PARAMS: dict[str, tuple[float, float, float]] = {
    "AAPL":  (190.00, 0.15, 0.25),
    "GOOGL": (175.00, 0.12, 0.28),
    "MSFT":  (415.00, 0.18, 0.22),
    "AMZN":  (185.00, 0.20, 0.30),
    "TSLA":  (250.00, 0.08, 0.55),
    "NVDA":  (875.00, 0.35, 0.50),
    "META":  (520.00, 0.25, 0.32),
    "JPM":   (200.00, 0.10, 0.20),
    "V":     (270.00, 0.12, 0.18),
    "NFLX":  (700.00, 0.15, 0.38),
}

# Sector-aware correlation matrix for the 10 default tickers (same order as TICKER_PARAMS).
# Tech stocks: high inter-correlation (0.55–0.70).
# Financials (JPM/V): high with each other (0.65), low with tech (0.22–0.35).
_DEFAULT_TICKERS = list(TICKER_PARAMS.keys())
CORRELATION_MATRIX = np.array([
    # AAPL   GOOGL  MSFT   AMZN   TSLA   NVDA   META   JPM    V      NFLX
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

P_EVENT = 0.002          # ~0.2% chance of shock per ticker per tick (~once per 4 min)
EVENT_REVERT_TICKS = 10  # ticks of suppressed drift after a shock (5 seconds)


class _TickerState(NamedTuple):
    price: float
    seed_price: float  # for session-relative change_pct
    drift: float       # per-tick drift (pre-computed)
    vol: float         # per-tick volatility (pre-computed)
    revert: int        # ticks remaining in post-shock mean reversion


class MarketSimulator(MarketDataSource):
    """Generates synthetic stock prices using correlated Geometric Brownian Motion.

    The 10 default tickers share a correlation matrix (Cholesky decomposed once
    at startup). Additional tickers added at runtime are assigned a default
    correlation of 0.3 with all others; the Cholesky factor is recomputed.
    """

    def __init__(self, cache: PriceCache) -> None:
        self._cache = cache
        self._tickers: list[str] = []
        self._states: dict[str, _TickerState] = {}
        self._cholesky: np.ndarray | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # MarketDataSource interface
    # ------------------------------------------------------------------

    async def start(self, tickers: list[str]) -> None:
        self._init_tickers(tickers)
        # Push initial prices to cache immediately so the app is never in a
        # "no prices yet" state when it starts serving requests.
        initial_updates = [
            PriceUpdate(
                ticker=t,
                price=s.price,
                prev_price=s.price,
                timestamp=datetime.now(tz=timezone.utc),
                change=0.0,
                change_pct=0.0,
            )
            for t, s in self._states.items()
        ]
        await self._cache.update(initial_updates)
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
            logger.debug("Simulator: added ticker %s", ticker)

    def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper()
        if ticker in self._states:
            del self._states[ticker]
            self._tickers = [t for t in self._tickers if t != ticker]
            self._rebuild_cholesky()
            logger.debug("Simulator: removed ticker %s", ticker)

    async def get_daily_bars(
        self, ticker: str, from_date: str, to_date: str
    ) -> list[DailyBar]:
        # Simulator has no historical data.
        return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_state(self, ticker: str) -> _TickerState:
        seed_price, annual_drift, annual_vol = TICKER_PARAMS.get(
            ticker,
            (100.0, 0.12, 0.30),  # sensible defaults for unknown tickers
        )
        dt = TICK_INTERVAL
        drift_per_tick = (annual_drift - 0.5 * annual_vol ** 2) * (dt / SECONDS_PER_YEAR)
        vol_per_tick = annual_vol * (dt / SECONDS_PER_YEAR) ** 0.5
        return _TickerState(
            price=seed_price,
            seed_price=seed_price,
            drift=drift_per_tick,
            vol=vol_per_tick,
            revert=0,
        )

    def _init_tickers(self, tickers: list[str]) -> None:
        self._tickers = [t.upper() for t in tickers]
        for ticker in self._tickers:
            self._states[ticker] = self._make_state(ticker)
        self._rebuild_cholesky()

    def _rebuild_cholesky(self) -> None:
        n = len(self._tickers)
        if n == 0:
            self._cholesky = None
            return

        default_idx = {t: i for i, t in enumerate(_DEFAULT_TICKERS)}
        C = np.eye(n)
        for i, ti in enumerate(self._tickers):
            for j, tj in enumerate(self._tickers):
                if i == j:
                    continue
                if ti in default_idx and tj in default_idx:
                    C[i, j] = CORRELATION_MATRIX[default_idx[ti], default_idx[tj]]
                else:
                    C[i, j] = 0.3  # default cross-asset correlation for unknown tickers

        try:
            self._cholesky = np.linalg.cholesky(C)
        except np.linalg.LinAlgError:
            # Fallback: identity (uncorrelated) if matrix is not positive-definite
            logger.warning("Correlation matrix not positive-definite; using uncorrelated normals")
            self._cholesky = np.eye(n)

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

        # Correlated standard normals
        z_raw = np.random.standard_normal(n)
        z = self._cholesky @ z_raw

        now = datetime.now(tz=timezone.utc)
        updates: list[PriceUpdate] = []
        new_states: dict[str, _TickerState] = {}

        for i, ticker in enumerate(self._tickers):
            state = self._states[ticker]

            # Shock event: random jump of ±0–5%
            if state.revert == 0 and np.random.random() < P_EVENT:
                shock_pct = np.random.uniform(-0.05, 0.05)
                new_price = max(state.price * (1.0 + shock_pct), 0.01)
                change = new_price - state.price
                session_change_pct = (new_price - state.seed_price) / state.seed_price * 100
                updates.append(PriceUpdate(
                    ticker=ticker,
                    price=round(new_price, 4),
                    prev_price=round(state.price, 4),
                    timestamp=now,
                    change=round(change, 4),
                    change_pct=round(session_change_pct, 4),
                ))
                new_states[ticker] = state._replace(price=new_price, revert=EVENT_REVERT_TICKS)
                continue

            # GBM update
            effective_drift = 0.0 if state.revert > 0 else state.drift
            log_return = effective_drift + state.vol * float(z[i])
            new_price = max(state.price * float(np.exp(log_return)), 0.01)
            change = new_price - state.price
            session_change_pct = (new_price - state.seed_price) / state.seed_price * 100

            updates.append(PriceUpdate(
                ticker=ticker,
                price=round(new_price, 4),
                prev_price=round(state.price, 4),
                timestamp=now,
                change=round(change, 4),
                change_pct=round(session_change_pct, 4),
            ))
            new_revert = max(state.revert - 1, 0)
            new_states[ticker] = state._replace(price=new_price, revert=new_revert)

        for ticker, state in new_states.items():
            self._states[ticker] = state

        return updates
```

### Simulator Behaviour Summary

| Property | Value |
|---|---|
| Update frequency | 500 ms |
| Typical price change per tick | 0.01%–0.05% |
| Shock probability | ~0.2% per ticker per tick |
| Shock magnitude | ±0–5% instantaneous |
| Post-shock drift | Suppressed for 5 s (10 ticks) |
| Tech-sector correlation | 0.45–0.70 |
| Financials ↔ tech correlation | 0.20–0.35 |
| `change_pct` meaning | % change since simulator start (session-relative) |

---

## 8. Massive API Client

`backend/app/market/massive_client.py`

### Endpoint Used

```
GET https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers
    ?tickers=AAPL,MSFT,GOOGL,...
Authorization: Bearer <API_KEY>
```

Returns `todaysChange`, `todaysChangePerc`, `day.c` (current price), `prevDay.c` (previous close), and `lastTrade.p` (most recent trade, plan-dependent).

### Full Implementation

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
    """Polls the Massive (formerly Polygon.io) REST snapshot endpoint.

    Poll interval guide:
      Free tier  (5 req/min):  poll_interval = 15.0 s  (safe default)
      Starter+   (unlimited):  poll_interval = 2.0–5.0 s

    Uses httpx.AsyncClient directly rather than the official 'massive' library
    because the official library is synchronous and would block the event loop.
    """

    def __init__(
        self,
        api_key: str,
        cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._cache = cache
        self._poll_interval = poll_interval
        self._tickers: set[str] = set()
        self._task: asyncio.Task | None = None
        self._headers = {"Authorization": f"Bearer {api_key}"}

    # ------------------------------------------------------------------
    # MarketDataSource interface
    # ------------------------------------------------------------------

    async def start(self, tickers: list[str]) -> None:
        self._tickers = {t.upper() for t in tickers}
        # Fetch initial snapshot synchronously before launching the loop,
        # so the cache is non-empty when the app begins serving requests.
        async with httpx.AsyncClient(timeout=15.0) as http:
            updates = await self._fetch_snapshots(http)
            if updates:
                await self._cache.update(updates)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "MassiveClient started, polling %d tickers every %.1fs",
            len(self._tickers),
            self._poll_interval,
        )

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

    async def get_daily_bars(
        self, ticker: str, from_date: str, to_date: str
    ) -> list[DailyBar]:
        url = f"{BASE_URL}/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}"
        params = {"adjusted": "true", "sort": "asc", "limit": 5000}
        async with httpx.AsyncClient(timeout=15.0) as http:
            try:
                response = await http.get(url, params=params, headers=self._headers)
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, Exception) as exc:
                logger.error("Failed to fetch daily bars for %s: %s", ticker, exc)
                return []

        bars: list[DailyBar] = []
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

    # ------------------------------------------------------------------
    # Internal polling loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        async with httpx.AsyncClient(timeout=10.0) as http:
            while True:
                await asyncio.sleep(self._poll_interval)
                if self._tickers:
                    updates = await self._fetch_snapshots(http)
                    if updates:
                        await self._cache.update(updates)

    async def _fetch_snapshots(self, http: httpx.AsyncClient) -> list[PriceUpdate]:
        if not self._tickers:
            return []

        url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers"
        params = {"tickers": ",".join(sorted(self._tickers))}
        try:
            response = await http.get(url, params=params, headers=self._headers)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 403:
                logger.error("Massive API: invalid API key (403). Stopping polling.")
                if self._task:
                    self._task.cancel()
                return []
            if status == 429:
                logger.warning("Massive API: rate limited (429). Backing off 60 s.")
                await asyncio.sleep(60)
            else:
                logger.error("Massive API: HTTP %d. Retrying in 5 s.", status)
                await asyncio.sleep(5)
            return []
        except httpx.RequestError as exc:
            logger.error("Massive API: network error: %s. Retrying in 5 s.", exc)
            await asyncio.sleep(5)
            return []

        if data.get("status") != "OK":
            logger.warning("Massive API returned non-OK status: %s", data.get("status"))
            return []

        return self._parse_snapshots(data)

    def _parse_snapshots(self, data: dict) -> list[PriceUpdate]:
        updates: list[PriceUpdate] = []
        for t in data.get("tickers", []):
            try:
                ticker_sym = t["ticker"]

                # Prefer lastTrade.p (most recent trade); fall back to day.c.
                last_trade = t.get("lastTrade") or {}
                price = last_trade.get("p") or t["day"]["c"]

                # Use prevDay.c as the daily reference price.
                prev_close = t["prevDay"]["c"]

                # Massive provides these directly — use them for accuracy.
                change = t.get("todaysChange", price - prev_close)
                change_pct = t.get("todaysChangePerc", (change / prev_close * 100) if prev_close else 0.0)

                ts_ns = t.get("updated", 0)
                timestamp = (
                    datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
                    if ts_ns
                    else datetime.now(tz=timezone.utc)
                )

                updates.append(PriceUpdate(
                    ticker=ticker_sym,
                    price=round(price, 4),
                    prev_price=round(prev_close, 4),
                    timestamp=timestamp,
                    change=round(change, 4),
                    change_pct=round(change_pct, 4),
                ))
            except (KeyError, TypeError, ZeroDivisionError) as exc:
                logger.warning(
                    "Massive API: failed to parse snapshot for %s: %s",
                    t.get("ticker", "?"),
                    exc,
                )
        return updates
```

### Watchlist Edge Case: Ticker Not Yet in Cache

When a ticker is added to the watchlist and the next Massive poll hasn't run yet, `cache.get(ticker)` returns `None`. The watchlist API route should return `null` for the price field in this case — **not** `0` or an error — and the frontend should display a loading state for that ticker.

---

## 9. Factory Function

`backend/app/market/factory.py`

```python
import os
import logging
from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(cache: PriceCache) -> MarketDataSource:
    """Return the correct MarketDataSource based on environment variables.

    Resolution order:
      1. MASSIVE_API_KEY set and non-empty → MassiveClient
      2. Otherwise → MarketSimulator

    Optional env vars:
      MASSIVE_POLL_INTERVAL  float seconds, default 15.0 (safe for free tier)
    """
    api_key = os.getenv("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveClient
        poll_interval = float(os.getenv("MASSIVE_POLL_INTERVAL", "15.0"))
        logger.info("Market data: Massive API (poll_interval=%.1fs)", poll_interval)
        return MassiveClient(api_key=api_key, cache=cache, poll_interval=poll_interval)

    from .simulator import MarketSimulator
    logger.info("Market data: built-in simulator (no MASSIVE_API_KEY)")
    return MarketSimulator(cache=cache)
```

---

## 10. FastAPI Integration

`backend/app/main.py` (market data portions shown)

```python
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI

from .market import PriceCache, create_market_data_source
from .database import get_tracked_tickers  # returns watchlist ∪ positions tickers

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                   "NVDA", "META", "JPM", "V", "NFLX"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = PriceCache()
    source = create_market_data_source(cache)

    # Load tickers from DB: union of watchlist and open position tickers.
    # Falls back to defaults if the DB doesn't exist yet (first run).
    tickers = await get_tracked_tickers() or DEFAULT_TICKERS

    await source.start(tickers)

    app.state.cache = cache
    app.state.market = source

    yield  # app is live

    await source.stop()


app = FastAPI(lifespan=lifespan)
```

`get_tracked_tickers()` (to be implemented in `backend/app/database.py`):

```python
async def get_tracked_tickers() -> list[str]:
    """Return the union of watchlist tickers and open position tickers."""
    watchlist = await db_fetchall("SELECT ticker FROM watchlist WHERE user_id='default'")
    positions = await db_fetchall(
        "SELECT ticker FROM positions WHERE user_id='default' AND quantity > 0"
    )
    all_tickers = {row["ticker"] for row in watchlist} | {row["ticker"] for row in positions}
    return list(all_tickers)
```

---

## 11. SSE Streaming Endpoint

`backend/app/routes/stream.py`

```python
import asyncio
import json
import logging
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..market import PriceCache

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/stream/prices")
async def stream_prices(request: Request):
    """Server-Sent Events endpoint for live price updates.

    On connect: immediately sends the full current cache state so the client
    has prices before the first tick fires.

    On subsequent updates: pushes each PriceUpdate as it arrives in the queue.

    On client disconnect: unsubscribes the queue and exits cleanly.

    Keepalive: sends an SSE comment line every 30 s if no updates arrive,
    which prevents proxies and browsers from timing out the connection.
    """
    cache: PriceCache = request.app.state.cache
    queue = cache.subscribe()

    async def event_generator():
        try:
            # Snapshot: send all current prices immediately on connect
            for update in cache.get_all().values():
                yield f"data: {json.dumps(update.to_dict())}\n\n"

            # Stream: push updates as they arrive
            while True:
                if await request.is_disconnected():
                    break
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(update.to_dict())}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            cache.unsubscribe(queue)
            logger.debug("SSE client disconnected")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )
```

### Frontend EventSource Usage

```typescript
const es = new EventSource("/api/stream/prices");

es.onmessage = (event) => {
  const update = JSON.parse(event.data);
  // update = { ticker, price, prev_price, change, change_pct, direction, timestamp }
  dispatch(priceUpdated(update));
};

es.onerror = () => {
  // EventSource retries automatically with exponential backoff.
  // Update connection status indicator to "reconnecting".
};
```

---

## 12. Watchlist API (Market Layer)

When a watchlist route adds or removes a ticker, it must coordinate with the market source and respect the `positions ∪ watchlist` scope rule.

```python
@router.post("/api/watchlist")
async def add_watchlist_ticker(body: dict, request: Request):
    ticker = body.get("ticker", "").upper().strip()
    if not ticker:
        raise HTTPException(status_code=422, detail="ticker is required")

    # Validate: basic sanity check (1–5 uppercase letters)
    import re
    if not re.fullmatch(r"[A-Z]{1,5}", ticker):
        raise HTTPException(status_code=422, detail=f"Invalid ticker: {ticker}")

    market: MarketDataSource = request.app.state.market
    cache: PriceCache = request.app.state.cache

    # Persist to DB (idempotent due to UNIQUE constraint)
    await db_execute(
        "INSERT OR IGNORE INTO watchlist (id, user_id, ticker, added_at) VALUES (?, 'default', ?, ?)",
        [str(uuid4()), ticker, datetime.utcnow().isoformat()],
    )

    # Register with market source so it starts tracking on the next cycle
    market.add_ticker(ticker)

    # Return current price (may be None if not yet in cache)
    price_update = cache.get(ticker)
    return {
        "ticker": ticker,
        "price": price_update.price if price_update else None,
        "change_pct": price_update.change_pct if price_update else None,
    }


@router.delete("/api/watchlist/{ticker}")
async def remove_watchlist_ticker(ticker: str, request: Request):
    ticker = ticker.upper()
    market: MarketDataSource = request.app.state.market

    # Remove from DB
    await db_execute(
        "DELETE FROM watchlist WHERE user_id='default' AND ticker=?", [ticker]
    )

    # Only stop tracking if the user has no open position in this ticker
    position_qty = await db_fetchone(
        "SELECT quantity FROM positions WHERE user_id='default' AND ticker=?", [ticker]
    )
    has_position = position_qty and position_qty["quantity"] > 0

    if not has_position:
        market.remove_ticker(ticker)

    return {"ticker": ticker, "removed": True}
```

---

## 13. Testing

### Unit Test: Simulator Price Generation

```python
# backend/tests/market/test_simulator.py
import asyncio
import pytest
from backend.app.market.cache import PriceCache
from backend.app.market.simulator import MarketSimulator


@pytest.mark.asyncio
async def test_simulator_updates_cache():
    cache = PriceCache()
    sim = MarketSimulator(cache)
    await sim.start(["AAPL", "MSFT"])

    # Wait for at least one tick
    await asyncio.sleep(0.6)
    await sim.stop()

    prices = cache.get_all()
    assert "AAPL" in prices
    assert "MSFT" in prices
    assert prices["AAPL"].price > 0
    assert prices["MSFT"].price > 0


@pytest.mark.asyncio
async def test_simulator_initial_snapshot_in_cache():
    """Cache must be non-empty immediately after start(), before any tick."""
    cache = PriceCache()
    sim = MarketSimulator(cache)
    await sim.start(["AAPL"])
    await sim.stop()  # stop immediately

    assert cache.get("AAPL") is not None


def test_simulator_add_remove_ticker():
    cache = PriceCache()
    sim = MarketSimulator(cache)

    sim._init_tickers(["AAPL"])
    assert "AAPL" in sim._states

    sim.add_ticker("PYPL")
    assert "PYPL" in sim._states
    assert len(sim._tickers) == 2

    sim.remove_ticker("PYPL")
    assert "PYPL" not in sim._states
    assert len(sim._tickers) == 1


def test_prices_always_positive():
    cache = PriceCache()
    sim = MarketSimulator(cache)
    sim._init_tickers(["AAPL", "TSLA", "NVDA"])

    for _ in range(500):
        updates = sim._compute_tick()
        for u in updates:
            assert u.price > 0, f"{u.ticker} price went non-positive: {u.price}"
```

### Unit Test: Massive Client Parsing

```python
# backend/tests/market/test_massive_client.py
import pytest
from datetime import timezone
from backend.app.market.cache import PriceCache
from backend.app.market.massive_client import MassiveClient

SAMPLE_RESPONSE = {
    "status": "OK",
    "count": 1,
    "tickers": [{
        "ticker": "AAPL",
        "todaysChange": 1.23,
        "todaysChangePerc": 0.65,
        "updated": 1605195918306274000,
        "day": {"o": 189.30, "h": 191.05, "l": 188.82, "c": 190.54, "v": 52341000, "vw": 190.12},
        "prevDay": {"o": 188.10, "h": 189.90, "l": 187.35, "c": 189.31, "v": 48200000, "vw": 188.75},
        "lastTrade": {"p": 190.54, "s": 100, "t": 1605195918306274000},
    }]
}


def test_parse_snapshots_uses_today_change_perc():
    cache = PriceCache()
    client = MassiveClient(api_key="test", cache=cache)
    updates = client._parse_snapshots(SAMPLE_RESPONSE)

    assert len(updates) == 1
    u = updates[0]
    assert u.ticker == "AAPL"
    assert u.price == 190.54      # from lastTrade.p
    assert u.prev_price == 189.31 # from prevDay.c
    assert u.change_pct == 0.65   # from todaysChangePerc
    assert u.direction == "up"


def test_parse_snapshots_missing_last_trade_falls_back_to_day_close():
    cache = PriceCache()
    client = MassiveClient(api_key="test", cache=cache)
    data = {
        "status": "OK",
        "tickers": [{
            "ticker": "MSFT",
            "todaysChange": 2.00,
            "todaysChangePerc": 0.50,
            "updated": 1605195918306274000,
            "day": {"c": 415.00, "o": 413.00, "h": 416.00, "l": 412.00, "v": 10000000, "vw": 414.50},
            "prevDay": {"c": 413.00},
            # no lastTrade field
        }]
    }
    updates = client._parse_snapshots(data)
    assert updates[0].price == 415.00  # fell back to day.c
```

### Integration Test: SSE Snapshot on Connect

```python
# backend/tests/test_stream.py
import asyncio
import json
import pytest
from httpx import AsyncClient, ASGITransport
from backend.app.main import app


@pytest.mark.asyncio
async def test_sse_sends_initial_snapshot():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        async with ac.stream("GET", "/api/stream/prices") as response:
            assert response.status_code == 200
            # Read first event
            first_line = ""
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    first_line = line
                    break
            data = json.loads(first_line.removeprefix("data: "))
            assert "ticker" in data
            assert "price" in data
            assert data["price"] > 0
```

### Manual Smoke Test

```python
# Run from backend/ directory: uv run python -m scripts.smoke_test_market
import asyncio
from backend.app.market.cache import PriceCache
from backend.app.market.simulator import MarketSimulator


async def main():
    cache = PriceCache()
    sim = MarketSimulator(cache)
    await sim.start(["AAPL", "MSFT", "TSLA"])

    print("Tick | AAPL        | MSFT        | TSLA")
    print("-" * 50)
    for tick in range(10):
        await asyncio.sleep(0.5)
        prices = cache.get_all()
        row = f"  {tick+1:2d} | "
        for sym in ["AAPL", "MSFT", "TSLA"]:
            p = prices.get(sym)
            row += f"${p.price:7.2f} {p.direction[0]:1s} | " if p else "  n/a       | "
        print(row)

    await sim.stop()


asyncio.run(main())
```

Expected output (prices will vary):

```
Tick | AAPL        | MSFT        | TSLA
--------------------------------------------------
   1 | $190.02 u | $415.08 u | $249.93 d |
   2 | $190.05 u | $415.01 d | $250.11 u |
  ...
```

---

## Summary

| Component | File | Role |
|---|---|---|
| `PriceUpdate`, `DailyBar` | `models.py` | Shared data structures |
| `PriceCache` | `cache.py` | Single source of truth; SSE fan-out |
| `MarketDataSource` | `interface.py` | ABC — enforces pluggable implementation |
| `MarketSimulator` | `simulator.py` | GBM synthetic prices, 500 ms ticks |
| `MassiveClient` | `massive_client.py` | REST polling, 2–15 s depending on plan |
| `create_market_data_source` | `factory.py` | Reads env vars, returns correct impl |
| Lifespan hook | `main.py` | Wires cache + source into app state |
| SSE endpoint | `routes/stream.py` | Pushes price events to browser clients |
