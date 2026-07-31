# Unified Market Data Interface

Design for `backend/app/market/` — the abstraction that lets the rest of FinAlly (SSE stream,
portfolio valuation, trade execution) read live prices without knowing whether they came from the
Massive API or the built-in simulator. Backed by the research in `MASSIVE_API.md`; simulator
internals are in `MARKET_SIMULATOR.md`.

## 1. Goals

- One interface, two implementations, selected purely by whether `MASSIVE_API_KEY` is set
  (`PLAN.md` §5)
- Downstream code (SSE stream, portfolio math) never branches on data source
- Survives Massive rate limits / outages without ever serving "no price" to the frontend
- Cheap to extend with a third source later (e.g. a different vendor) without touching callers

## 2. Shape of the Data: `PriceUpdate`

A single immutable record represents "the latest known state of one ticker":

```python
# models.py
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

class Direction(str, Enum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"

@dataclass(frozen=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: datetime
    direction: Direction

    @property
    def change(self) -> float:
        return self.price - self.previous_price

    @property
    def change_percent(self) -> float:
        if self.previous_price == 0:
            return 0.0
        return (self.change / self.previous_price) * 100
```

Both the simulator and the Massive client produce this same type — it's the only thing that
crosses the boundary out of `app/market/`.

## 3. The Abstract Interface

```python
# interface.py
from abc import ABC, abstractmethod

class MarketDataSource(ABC):
    """A background process that keeps a PriceCache updated for a set of tickers."""

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing updates for the given tickers (writes into the shared cache)."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop background work cleanly (cancel tasks, close HTTP/WS clients)."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Start tracking a new ticker without restarting the whole source."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker (e.g. removed from the watchlist)."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Currently tracked tickers."""
```

Both `SimulatorDataSource` and `MassiveDataSource` implement this. Neither exposes anything else
publicly — no HTTP client, no simulation state — so callers can't accidentally couple to one
implementation's internals.

## 4. The Shared Price Cache

A single in-memory, thread/async-safe store sits between the data source and every consumer
(SSE endpoint, portfolio valuation, trade execution). Producers write, everyone else reads:

```python
# cache.py
import asyncio

class PriceCache:
    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._version = 0
        self._lock = asyncio.Lock()

    async def set(self, update: PriceUpdate) -> None:
        async with self._lock:
            self._prices[update.ticker] = update
            self._version += 1

    def get(self, ticker: str) -> PriceUpdate | None:
        return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        u = self.get(ticker)
        return u.price if u else None

    def get_all(self) -> dict[str, PriceUpdate]:
        return dict(self._prices)

    @property
    def version(self) -> int:
        return self._version
```

The `version` counter lets the SSE endpoint cheaply detect "has anything changed since I last
looked" without diffing the whole dict on every tick — see §7.

This design point matters regardless of which source is active: **the cache always holds the last
known value for every ticker.** Nothing ever gets deleted except by an explicit `remove_ticker`.
That's what makes Massive rate limits and transient outages invisible to the frontend — see §6.

## 5. Selecting an Implementation: the Factory

```python
# factory.py
import os

def create_market_data_source(cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(cache, api_key=api_key)
    return SimulatorDataSource(cache)
```

This is the only place that reads the environment variable. Everything else — app startup, the
SSE router, tests — takes a `MarketDataSource` and doesn't care which concrete class it got.

Startup wiring (e.g. FastAPI lifespan):

```python
cache = PriceCache()
source = create_market_data_source(cache)
watchlist = load_watchlist_tickers()  # from SQLite
await source.start(watchlist)
```

## 6. `MassiveDataSource`: REST Polling Implementation

Per `MASSIVE_API.md` §3, the free Massive tier allows 5 requests/minute, so this implementation:

- Polls the **batched multi-ticker snapshot** endpoint (`GET /v2/snapshot/.../tickers?tickers=...`)
  — one HTTP call covers the entire watchlist regardless of size
- Uses a poll interval read from an env var (default 15s, matching the free-tier budget of 5/min
  with headroom); paid-tier users can lower it via `MASSIVE_POLL_INTERVAL_SECONDS`
- Runs as a single `asyncio` background task with a `while running: await asyncio.sleep(interval)`
  loop — no separate task per ticker
- On each poll: parses the batch response, and for each ticker builds a `PriceUpdate` using the
  *previous* cached price (or `prevDay.c` on the very first poll) as `previous_price`, so direction
  and change are always computed tick-over-tick rather than against a stale baseline
- On a per-ticker `NOT_FOUND`/error entry in the batch response: skip that ticker this round,
  leave its last cached value untouched, log a warning
- On a request failure (timeout, `429`, 5xx): catch it, log, skip this poll cycle entirely, leave
  the whole cache untouched, retry on the next scheduled poll — **never propagate the failure to
  callers or blank out prices**
- `add_ticker`/`remove_ticker` just mutate the tracked ticker set consulted on the *next* poll
  (no need to restart the task or make an extra request)

```python
# massive_client.py (sketch)
class MassiveDataSource(MarketDataSource):
    def __init__(self, cache: PriceCache, api_key: str,
                 poll_interval: float = 15.0) -> None:
        self._cache = cache
        self._client = RESTClient(api_key=api_key)
        self._interval = poll_interval
        self._tickers: set[str] = set()
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self, tickers: list[str]) -> None:
        self._tickers = set(tickers)
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._poll_once()
            except Exception:
                log.warning("Massive poll failed; keeping last known prices", exc_info=True)
            await asyncio.sleep(self._interval)

    async def _poll_once(self) -> None:
        if not self._tickers:
            return
        snapshot = await asyncio.to_thread(
            self._client.get_snapshot_all, "stocks", tickers=list(self._tickers)
        )
        for entry in snapshot:
            if getattr(entry, "error", None):
                continue
            prev = self._cache.get_price(entry.ticker)
            baseline = prev if prev is not None else entry.prev_day.close
            price = entry.day.close or entry.last_trade.price
            await self._cache.set(PriceUpdate(
                ticker=entry.ticker,
                price=price,
                previous_price=baseline,
                timestamp=datetime.now(timezone.utc),
                direction=_direction(price, baseline),
            ))
```

The blocking `massive` client call is offloaded via `asyncio.to_thread` since it's a synchronous
`requests`-based SDK, keeping the event loop free.

## 7. Consumers of the Cache

### SSE stream (`GET /api/stream/prices`)

```python
async def _generate_events(cache: PriceCache) -> AsyncGenerator[str, None]:
    last_seen_version = -1
    while True:
        if cache.version != last_seen_version:
            last_seen_version = cache.version
            for update in cache.get_all().values():
                yield f"data: {json.dumps(asdict(update), default=str)}\n\n"
        await asyncio.sleep(0.5)
```

Polling the cache's `version` at ~500ms gives the frontend the smooth, frequent cadence described
in `PLAN.md` §6/§10, decoupled from however slowly the *upstream* Massive poll actually refreshes
data — between real updates the cache simply reports the same values again, which is harmless
(the frontend's flash animation only fires on an actual price change).

### Portfolio valuation / trade execution

Both call `cache.get_price(ticker)` synchronously — a plain dict lookup, no I/O, so trade execution
is never blocked on network calls to Massive.

## 8. Testing Strategy

- `MarketDataSource` is an ABC — a lightweight `FakeDataSource` (or just `SimulatorDataSource`
  with a fixed seed) can stand in for `MassiveDataSource` in tests that need a data source but
  aren't testing Massive-specific parsing
- `MassiveDataSource` tests mock the `massive.RESTClient` methods (or the underlying HTTP call)
  and assert: successful batch parsing, per-ticker `NOT_FOUND` handling, and that a raised
  exception during polling leaves the cache untouched rather than clearing it
- `factory.py` tests assert env-var presence/absence selects the right class, using
  `monkeypatch.setenv`/`delenv`

## 9. File Layout

```
backend/app/market/
├── __init__.py         # re-exports PriceCache, MarketDataSource, create_market_data_source
├── models.py           # PriceUpdate, Direction
├── interface.py         # MarketDataSource ABC
├── cache.py             # PriceCache
├── factory.py           # create_market_data_source()
├── simulator.py          # SimulatorDataSource — see MARKET_SIMULATOR.md
├── massive_client.py     # MassiveDataSource — this document, §6
└── stream.py             # SSE router factory consuming a PriceCache — §7
```
