# Market Data Interface Design

Unified Python interface for market data in FinAlly. Two implementations — a GBM simulator and the Massive API (see `MASSIVE_API.md`) — sit behind one abstract interface. All downstream code (SSE streaming, portfolio valuation, trade execution) is source-agnostic: it only ever talks to a `PriceCache`, never to a data source directly.

This document reflects the actual implementation in `backend/app/market/` (built, tested — 73 tests, 84% coverage — and code-reviewed; see `planning/MARKET_DATA_SUMMARY.md`).

## Core Data Model

`PriceUpdate` (`models.py`) is the only object that leaves the market data layer:

```python
from dataclasses import dataclass, field
import time

@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""
    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float: ...          # round(price - previous_price, 4)

    @property
    def change_percent(self) -> float: ...  # round(pct change, 4); 0.0 if previous_price == 0

    @property
    def direction(self) -> str: ...          # "up" | "down" | "flat"

    def to_dict(self) -> dict: ...           # JSON-serializable, used directly by SSE
```

`change`, `change_percent`, and `direction` are **computed properties**, not stored fields — there's only one source of truth (`price` vs `previous_price`), so they can't drift out of sync.

## Abstract Interface

```python
from abc import ABC, abstractmethod

class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...
    @abstractmethod
    async def add_ticker(self, ticker: str) -> None: ...
    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None: ...
    @abstractmethod
    def get_tickers(self) -> list[str]: ...
```

Lifecycle contract: `start()` is called exactly once; `stop()` is safe to call multiple times (idempotent); after `stop()`, the source writes nothing further to the cache.

Two concrete implementations: `SimulatorDataSource` (`simulator.py`, wraps `GBMSimulator` — see `MARKET_SIMULATOR.md`) and `MassiveDataSource` (`massive_client.py`, wraps the Massive `RESTClient`).

## Price Cache

The shared, thread-safe store both data sources write to and every reader (SSE stream, portfolio valuation, trade execution) reads from.

```python
class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker."""

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price. Computes previous_price from whatever was
        cached before (or price itself, on first write => direction='flat').
        Rounds price to 2 decimals. Bumps the version counter."""

    def get(self, ticker: str) -> PriceUpdate | None: ...
    def get_price(self, ticker: str) -> float | None: ...   # convenience: just the float
    def get_all(self) -> dict[str, PriceUpdate]: ...         # shallow-copy snapshot
    def remove(self, ticker: str) -> None: ...

    @property
    def version(self) -> int: ...   # monotonically increasing; bumped on every update()
```

Design points worth calling out:
- **Single point of truth.** Producers (`SimulatorDataSource` / `MassiveDataSource`) write; consumers read. Neither side is coupled to the other's implementation.
- **`version` powers change detection.** The SSE endpoint doesn't push on a fixed clock alone — it compares `PriceCache.version` between ticks and only serializes/sends when something actually changed. See `stream.py`.
- **Rounding happens once, at write time**, in `update()` — every reader downstream gets already-clean 2-decimal prices.
- **Thread-safety matters here specifically** because `MassiveDataSource` runs its (synchronous) API calls via `asyncio.to_thread`, so cache writes can arrive from a worker thread while the event loop thread is reading.

## Factory Function

Selects the data source at process startup based on environment:

```python
import os

def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """MASSIVE_API_KEY set and non-empty -> MassiveDataSource.
    Otherwise -> SimulatorDataSource. Returns an unstarted source;
    caller must await source.start(tickers)."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    return SimulatorDataSource(price_cache=price_cache)
```

Both branches are plain top-level imports in `factory.py` — `massive` is a required dependency of the backend (`massive>=1.0.0` in `pyproject.toml`), not an optional/lazy one, so there's no import-time branching to worry about.

## Massive Implementation

`MassiveDataSource` (`massive_client.py`) polls the batched snapshot endpoint on a timer (default 15s — the free-tier-safe interval; see `MASSIVE_API.md`):

```python
class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval: float = 15.0): ...

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()          # immediate first poll — no cold-start delay
        self._task = asyncio.create_task(self._poll_loop())

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return
        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots)  # sync client off the event loop
            for snap in snapshots:
                self._cache.update(
                    ticker=snap.ticker,
                    price=snap.last_trade.price,
                    timestamp=snap.last_trade.timestamp / 1000.0,  # ms -> seconds
                )
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # swallow and retry next interval — 401/429/network errors must not kill the loop

    def _fetch_snapshots(self) -> list:
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

Notes:
- The synchronous `massive` client runs via `asyncio.to_thread` so a slow/blocked HTTP call never stalls the event loop (and therefore never stalls the SSE stream or trade execution for other users of the same process).
- A malformed individual snapshot (missing `last_trade`, etc.) is caught and skipped per-ticker rather than aborting the whole poll — one bad ticker shouldn't blank out the other nine.
- `add_ticker` / `remove_ticker` just mutate the in-memory ticker list; the new ticker's price appears on the *next* scheduled poll (there's no per-ticker on-demand fetch).

## Simulator Implementation

`SimulatorDataSource` (`simulator.py`) wraps `GBMSimulator` (full math and correlation model in `MARKET_SIMULATOR.md`) in a fast (~500ms) async loop:

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5, event_probability: float = 0.001): ...

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        for ticker in tickers:                       # seed the cache immediately —
            price = self._sim.get_price(ticker)       # SSE has data on the very first poll,
            if price is not None:                      # no visible blank/loading state
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")   # never let one bad step kill the loop
            await asyncio.sleep(self._interval)
```

`add_ticker` seeds the cache immediately too (same reasoning), rather than waiting for the next `step()`.

## SSE Streaming

`create_stream_router(price_cache)` (`stream.py`) returns a FastAPI `APIRouter` exposing `GET /api/stream/prices`. The generator polls `PriceCache.version` every 500ms and only serializes + sends when it changed — no redundant payloads when nothing moved between checks:

```python
async def _generate_events(price_cache: PriceCache, request: Request, interval: float = 0.5):
    yield "retry: 1000\n\n"   # tells EventSource to auto-reconnect after 1s on drop
    last_version = -1
    while True:
        if await request.is_disconnected():
            break
        current_version = price_cache.version
        if current_version != last_version:
            last_version = current_version
            prices = price_cache.get_all()
            if prices:
                data = {ticker: update.to_dict() for ticker, update in prices.items()}
                yield f"data: {json.dumps(data)}\n\n"
        await asyncio.sleep(interval)
```

`X-Accel-Buffering: no` is set on the response so a proxy (e.g. nginx) in front of the app doesn't buffer the stream.

## File Structure (as built)

```
backend/
  app/
    market/
      __init__.py          # public API: PriceUpdate, PriceCache, MarketDataSource,
                            # create_market_data_source, create_stream_router
      models.py             # PriceUpdate
      interface.py           # MarketDataSource ABC
      cache.py               # PriceCache
      factory.py              # create_market_data_source()
      massive_client.py       # MassiveDataSource
      simulator.py            # GBMSimulator + SimulatorDataSource
      seed_prices.py           # SEED_PRICES, TICKER_PARAMS, correlation constants
      stream.py                # create_stream_router() — SSE endpoint factory
```

Import surface for the rest of the backend (per `backend/CLAUDE.md`):

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source, create_stream_router
```

## Lifecycle

1. **App startup**: create a `PriceCache`, call `create_market_data_source(price_cache)`, then `await source.start(initial_tickers)`.
2. **Watchlist changes**: `await source.add_ticker(t)` / `await source.remove_ticker(t)`.
3. **SSE streaming**: `create_stream_router(price_cache)` mounted once; reads `PriceCache.get_all()` whenever `version` changes.
4. **Trade execution / portfolio valuation**: read the current price via `price_cache.get_price(ticker)`.
5. **App shutdown**: `await source.stop()`.

## Known Follow-ups

From the code review (`planning/archive/MARKET_DATA_REVIEW.md`), still open / worth knowing about if extending this layer:
- `stream.py` has no dedicated SSE integration test (needs an ASGI test client); coverage on that module is intentionally low (31%).
- `PriceCache.version`'s getter isn't lock-guarded — safe under CPython's GIL today, would need revisiting on a no-GIL build.
- The Massive test suite requires the `massive` package to be installed to run cleanly (mocks target module-level names that only exist once `massive` is importable).
