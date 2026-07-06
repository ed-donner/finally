# Market Data Interface Design

Unified Python interface for market data in FinAlly. Two implementations (simulator and Massive API) behind one abstract interface. All downstream code — SSE streaming, price cache, portfolio valuation — is source-agnostic.

This document describes the interface **as implemented** in `backend/app/market/`. It supersedes `planning/archive/MARKET_INTERFACE.md`, which was a pre-implementation sketch; a few details below (the `PriceUpdate` shape, cache versioning, immediate-first-poll behavior) evolved during implementation and are captured here.

## Core Data Model

`backend/app/market/models.py`:

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
        """Serialize for JSON / SSE transmission."""
        ...
```

`change`, `change_percent`, and `direction` are **computed properties**, not stored fields — derived once from `price`/`previous_price` rather than duplicated as separate cache-mutable state. This is the only data structure that leaves the market data layer; everything downstream works with `PriceUpdate` objects (or their `.to_dict()` form over SSE).

## Abstract Interface

`backend/app/market/interface.py`:

```python
from abc import ABC, abstractmethod

class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.
        Must be called exactly once."""

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

Both implementations write to a shared `PriceCache`. The interface does **not** return prices directly — it pushes updates into the cache on its own schedule.

## Price Cache

`backend/app/market/cache.py`. Thread-safe (guarded by a `threading.Lock`, not an asyncio lock — both `SimulatorDataSource`'s event-loop task and any future non-async writer can share it safely) in-memory store that data sources write to and the SSE streamer reads from.

```python
class PriceCache:
    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # bumped on every update

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price. Computes previous_price from the prior entry
        (or == price on first write, so direction is 'flat' initially)."""
        ...

    def get(self, ticker: str) -> PriceUpdate | None: ...
    def get_all(self) -> dict[str, PriceUpdate]: ...          # shallow copy snapshot
    def get_price(self, ticker: str) -> float | None: ...      # convenience accessor
    def remove(self, ticker: str) -> None: ...

    @property
    def version(self) -> int:
        """Monotonically increasing counter, bumped on every update() call."""
```

The `version` counter exists specifically so the SSE endpoint can cheaply detect "did anything change since I last checked" without diffing dictionaries — see Integration with SSE below. This is a refinement over the original sketch, which had no change-detection mechanism.

## Factory Function

`backend/app/market/factory.py`. Selects the data source at startup based on environment:

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """MASSIVE_API_KEY set and non-empty -> MassiveDataSource. Otherwise -> SimulatorDataSource.
    Returns an unstarted source; caller must await source.start(tickers)."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        return SimulatorDataSource(price_cache=price_cache)
```

## Massive Implementation

`backend/app/market/massive_client.py` — `MassiveDataSource`. Polls `get_snapshot_all()` on a timer (default 15s, matching the free-tier rate limit; see `planning/MASSIVE_API.md`).

Key implementation details not obvious from the interface alone:
- `start()` does an **immediate first poll** before starting the background loop, so the cache has data right away instead of waiting a full interval
- The synchronous `RESTClient` call runs via `asyncio.to_thread` so it doesn't block the event loop
- `_poll_once` wraps each snapshot's field extraction in `try/except (AttributeError, TypeError)` per-ticker, and the whole poll in a broader `try/except Exception` — a single bad snapshot or a transient API failure doesn't kill the poller
- The timestamp conversion reads `snap.last_trade.sip_timestamp / 1_000_000_000` (nanoseconds → seconds). This was previously `snap.last_trade.timestamp / 1000.0`, referencing a field that doesn't exist on the real Massive `LastTrade` model — see "Fixed Issue" in `planning/MASSIVE_API.md` for the history; it's now fixed and covered by a `spec_set`-mocked test.

```python
async def _poll_once(self) -> None:
    if not self._tickers or not self._client:
        return
    try:
        snapshots = await asyncio.to_thread(self._fetch_snapshots)
        for snap in snapshots:
            try:
                self._cache.update(
                    ticker=snap.ticker,
                    price=snap.last_trade.price,
                    timestamp=snap.last_trade.sip_timestamp / 1_000_000_000,  # ns -> seconds
                )
            except (AttributeError, TypeError) as e:
                logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
    except Exception as e:
        logger.error("Massive poll failed: %s", e)  # retried on next interval
```

## Simulator Implementation

`backend/app/market/simulator.py` — `SimulatorDataSource` wraps `GBMSimulator` (see `planning/MARKET_SIMULATOR.md` for the math). Runs a background asyncio task calling `.step()` every `update_interval` seconds (default 0.5s).

```python
class SimulatorDataSource(MarketDataSource):
    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed the cache immediately so SSE has data before the first tick
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def add_ticker(self, ticker: str) -> None:
        self._sim.add_ticker(ticker)
        price = self._sim.get_price(ticker)
        if price is not None:
            self._cache.update(ticker=ticker, price=price)  # seed immediately, don't wait for next tick

    async def _run_loop(self) -> None:
        while True:
            try:
                prices = self._sim.step()
                for ticker, price in prices.items():
                    self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")  # loop survives a bad step
            await asyncio.sleep(self._interval)
```

Both `start()` and `add_ticker()` write an initial price into the cache synchronously rather than waiting for the next scheduled tick — this matters for the frontend, since a newly-added ticker should show a price immediately rather than a blank cell for up to 500ms.

## Integration with SSE

`backend/app/market/stream.py` — `create_stream_router(price_cache)` returns a FastAPI router exposing `GET /api/stream/prices`. The generator polls every 500ms but only emits an event when `price_cache.version` has changed since the last check, avoiding redundant identical payloads:

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

Per `planning/PLAN.md` §6, the server is meant to push at a fixed ~500ms cadence "regardless of the market data source," repeating the last-known price between Massive updates so the connection and frontend state stay consistent even when the underlying data hasn't changed for up to 15s. The version-gated implementation above is a deliberate refinement: it only emits a new SSE `data:` event when the price *actually* changed, rather than resending an identical payload every 500ms. This still satisfies the spec's intent (SSE connection stays alive via `is_disconnected()` polling every interval; the frontend's `EventSource` keeps its connection open regardless of event frequency) while avoiding wasted bandwidth on genuinely unchanged prices. It does mean the client won't receive a redundant heartbeat event every single tick — worth knowing if a future frontend change relies on tick-frequency events rather than change-frequency events.

## File Structure

```
backend/
  app/
    market/
      __init__.py            # Public API: PriceCache, PriceUpdate, MarketDataSource,
                              # create_market_data_source, create_stream_router
      models.py               # PriceUpdate dataclass
      cache.py                 # PriceCache
      interface.py             # MarketDataSource ABC
      factory.py                # create_market_data_source()
      massive_client.py         # MassiveDataSource
      simulator.py               # GBMSimulator + SimulatorDataSource
      seed_prices.py              # SEED_PRICES, TICKER_PARAMS, correlation constants
      stream.py                    # create_stream_router() — SSE endpoint factory
```

## Lifecycle

1. **App startup**: Create `PriceCache`, call `create_market_data_source(price_cache)`, then `await source.start(initial_tickers)`
2. **Watchlist changes**: Call `source.add_ticker()` or `source.remove_ticker()`
3. **SSE streaming**: `create_stream_router(cache)` mounted once at startup; each client connection reads from `PriceCache` independently
4. **Trade execution**: Reads current price via `PriceCache.get_price(ticker)`
5. **App shutdown**: `await source.stop()`

## Usage for Downstream Code

```python
from app.market import PriceCache, create_market_data_source

cache = PriceCache()
source = create_market_data_source(cache)  # reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", "MSFT", ...])

update = cache.get("AAPL")          # PriceUpdate or None
price = cache.get_price("AAPL")     # float or None
all_prices = cache.get_all()        # dict[str, PriceUpdate]

await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")

await source.stop()
```

## Test Coverage

73 tests across 6 modules in `backend/tests/market/`, 84% overall coverage. `massive_client.py` sits at 56% — expected, since its API-calling methods are exercised against mocks rather than the real Massive service. That gap is what had let the timestamp bug described above ship undetected; `test_massive.py` now mocks `last_trade` with `spec_set=LastTrade` (the real model class) instead of a bare `MagicMock()`, so a future field-name mismatch like this one fails the test suite instead of passing silently. See `planning/MASSIVE_API.md` for the fix history.
