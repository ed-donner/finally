# Market Data Interface Design

Unified Python interface for market data in FinAlly. Two implementations — `SimulatorDataSource` and `MassiveDataSource` — behind one abstract `MarketDataSource` interface, selected at startup by `create_market_data_source()`. All downstream code (SSE streaming, portfolio valuation, trade execution) is source-agnostic: it only ever talks to the shared `PriceCache`, never to a data source directly.

This document describes the interface as implemented today in `backend/app/market/` (`interface.py`, `cache.py`, `models.py`, `factory.py`, `simulator.py`, `massive_client.py`, `stream.py`), and calls out one place where the implementation is behind the project spec (`planning/PLAN.md` §6).

## Core Data Model (`models.py`)

```python
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
        elif self.price < self.previous_price:
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

`change`, `change_percent`, and `direction` are computed properties rather than stored fields — the class stays a minimal, immutable fact (`ticker`, `price`, `previous_price`, `timestamp`) and derives the display fields on read. This is the only object type that leaves the market data layer; SSE payloads and history responses are both built from `to_dict()`.

## Abstract Interface (`interface.py`)

```python
from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers. Call exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. Also removes it from the PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

Both implementations own a background `asyncio.Task` (an internal poll/step loop) started in `start()` and cancelled in `stop()`. The interface intentionally has no `get_price()` method — prices are pulled from `PriceCache`, not pushed synchronously through this interface, so callers never block on a network round trip or a simulation step.

## Price Cache (`cache.py`)

Thread-safe in-memory store both data sources write to and the SSE streamer / portfolio code reads from.

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker."""

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # bumped on every update; lets SSE detect "anything changed"

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
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

    def get_all(self) -> dict[str, PriceUpdate]:
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        return self._version
```

A `threading.Lock` (not an `asyncio.Lock`) is deliberate: `MassiveDataSource` calls the synchronous Massive SDK inside `asyncio.to_thread`, so cache writes can happen from a worker thread as well as the event loop. The `version` counter lets the SSE generator (`stream.py`) cheaply skip re-serializing and re-sending the full price map when nothing has changed between its poll ticks, instead of diffing the dict.

### ⚠️ Gap vs. `planning/PLAN.md` §6: no rolling history buffer yet

The project spec says:

> A single background task ... writes to an in-memory `PriceCache`, which holds, per ticker: the latest price, previous price, timestamp, **and a bounded rolling history buffer (most recent 500 points)** ... SSE streams and the history endpoint (`GET /api/market/history/{ticker}`) both read from this same cache.

**As implemented today, `PriceCache` only stores the single latest `PriceUpdate` per ticker** — no history buffer, and `GET /api/market/history/{ticker}` does not exist yet anywhere in `backend/app/`. This means sparkline pre-population and the main chart's "seed from history, then extend from SSE" behavior (PLAN.md §10) currently have no server-side data source to call on load.

This is the one place this document intentionally diverges from "describe what's implemented" — the recommended shape, consistent with the rest of the design, is a small addition to `PriceCache` rather than a new component:

```python
from collections import deque

class PriceCache:
    def __init__(self, history_size: int = 500) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._history: dict[str, deque[PriceUpdate]] = {}
        self._history_size = history_size
        self._lock = Lock()
        self._version: int = 0

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        with self._lock:
            ...
            self._prices[ticker] = update
            self._history.setdefault(ticker, deque(maxlen=self._history_size)).append(update)
            self._version += 1
            return update

    def get_history(self, ticker: str) -> list[PriceUpdate]:
        with self._lock:
            return list(self._history.get(ticker, ()))
```

`deque(maxlen=...)` gives O(1) bounded appends for free instead of manual list slicing. `remove()` should also drop `self._history.pop(ticker, None)` to match. The SSE update cadence is ~500ms (`SimulatorDataSource`'s default `update_interval`), so a 500-point buffer covers roughly 4 minutes of simulator history per ticker — for Massive's 15s poll interval it covers roughly 2 hours, which is plenty for a sparkline. `GET /api/market/history/{ticker}` (PLAN.md §8) is then a thin FastAPI route: `return [u.to_dict() for u in price_cache.get_history(ticker)]`, 404 if the ticker isn't tracked.

## Factory (`factory.py`)

Selects the data source at startup based on `MASSIVE_API_KEY`:

```python
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """MASSIVE_API_KEY set and non-empty -> MassiveDataSource, else -> SimulatorDataSource.

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

`.strip()` matters: an `.env` file with `MASSIVE_API_KEY=` (present but empty) must fall through to the simulator, not attempt a Massive connection with an empty key.

## `MassiveDataSource` (`massive_client.py`)

Polls the Massive snapshot endpoint for the tracked ticker set — see `MASSIVE_API.md` for the wire-level details and confirmed issues with the current field/method usage (`get_snapshot_all` call shape, `last_trade` timestamp field name, free-tier plan restrictions). Structurally:

```python
class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval: float = 15.0) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()  # immediate first poll so the cache isn't empty on connect
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return
        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            for snap in snapshots:
                try:
                    self._cache.update(ticker=snap.ticker, price=snap.last_trade.price, ...)
                except (AttributeError, TypeError):
                    ...  # skip malformed snapshot, keep going
        except Exception:
            ...  # log and let the next poll cycle retry — never crash the loop
```

Two resilience choices worth keeping in any future rewrite: an **immediate poll inside `start()`** (so the SSE stream has data on the very first client connection, not just after the first `poll_interval` elapses), and a **per-snapshot try/except inside the batch loop** (one malformed ticker in a 20-ticker response doesn't drop the other 19).

## `SimulatorDataSource` (`simulator.py`)

Wraps a `GBMSimulator` (full math and structure in `MARKET_SIMULATOR.md`) in the same async-loop shape:

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5, event_probability: float = 0.001) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

Both implementations seed the cache synchronously inside `start()` before the background task exists, for the same reason: a client that connects to the SSE stream immediately after startup should see prices on its first tick, not an empty payload.

## Integration with SSE (`stream.py`)

`create_stream_router(price_cache)` returns a FastAPI `APIRouter` (a factory, not a module-level router, so the cache is injected rather than reached via a global). The generator polls `price_cache.version` every 500ms and only serializes/sends when it has changed:

```python
async def _generate_events(price_cache: PriceCache, request: Request, interval: float = 0.5):
    yield "retry: 1000\n\n"
    last_version = -1
    while True:
        if await request.is_disconnected():
            break
        current_version = price_cache.version
        if current_version != last_version:
            last_version = current_version
            prices = price_cache.get_all()
            if prices:
                yield f"data: {json.dumps({t: u.to_dict() for t, u in prices.items()})}\n\n"
        await asyncio.sleep(interval)
```

## Tracked Ticker Set

Per PLAN.md §6, the set passed to `start()` / `add_ticker()` / `remove_ticker()` must always be **watchlist ∪ positions.ticker** for the user, not just the watchlist — this keeps a live price available for the positions table and portfolio valuation even if a held ticker is removed from the watchlist. Neither `MarketDataSource` implementation enforces this itself; it's the responsibility of the calling code (wherever watchlist/position mutations happen) to recompute the union and call `add_ticker`/`remove_ticker` accordingly, only removing a ticker once it's in neither set.

## File Structure

```
backend/
  app/
    market/
      __init__.py            # Public API re-exports
      models.py               # PriceUpdate dataclass
      interface.py             # MarketDataSource ABC
      cache.py                 # PriceCache (thread-safe, latest price + version counter)
      factory.py                # create_market_data_source()
      massive_client.py         # MassiveDataSource
      simulator.py               # GBMSimulator + SimulatorDataSource
      seed_prices.py              # SEED_PRICES, TICKER_PARAMS, correlation constants
      stream.py                    # create_stream_router() — SSE endpoint factory
```

## Lifecycle

1. **App startup**: create `PriceCache`, call `create_market_data_source(price_cache)`, then `await source.start(initial_tickers)` where `initial_tickers` is the watchlist ∪ positions union at boot
2. **Watchlist/position changes**: recompute the tracked set, call `source.add_ticker()` / `source.remove_ticker()` as needed
3. **SSE streaming**: `GET /api/stream/prices` reads `PriceCache.get_all()` on a 500ms poll, gated by `version`
4. **History pre-population** *(spec'd, not yet implemented — see gap above)*: `GET /api/market/history/{ticker}` reads `PriceCache.get_history(ticker)`
5. **Trade execution**: reads current price via `PriceCache.get(ticker)` / `get_price(ticker)`
6. **App shutdown**: `await source.stop()`
