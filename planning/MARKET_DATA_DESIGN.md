# Market Data Backend — Detailed Design

This document provides the complete implementation blueprint for the FinAlly market data subsystem. It covers every module in `backend/app/market/`, with full code listings, design rationale, and integration patterns.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Module Layout](#2-module-layout)
3. [PriceUpdate Model](#3-priceupdate-model)
4. [PriceCache](#4-pricecache)
5. [MarketDataSource Interface](#5-marketdatasource-interface)
6. [GBM Simulator](#6-gbm-simulator)
7. [Massive API Client](#7-massive-api-client)
8. [Factory](#8-factory)
9. [SSE Streaming](#9-sse-streaming)
10. [Seed Data & Configuration](#10-seed-data--configuration)
11. [Package Exports](#11-package-exports)
12. [FastAPI Integration](#12-fastapi-integration)
13. [Testing Strategy](#13-testing-strategy)
14. [Error Handling & Resilience](#14-error-handling--resilience)

---

## 1. Architecture Overview

The market data subsystem follows a **Strategy pattern** with a shared cache as the integration point. One data source writes prices; many consumers read them.

```
Environment Variable (MASSIVE_API_KEY)
    │
    ▼
┌──────────────────────────┐
│ create_market_data_source │──── key set ──────▶ MassiveDataSource
│ (factory.py)              │                     (polls REST every 15s)
│                           │──── no key ────────▶ SimulatorDataSource
└──────────────────────────┘                     (GBM ticks every 500ms)
                                    │
                                    ▼
                             ┌─────────────┐
                             │ PriceCache   │ ◀── thread-safe, in-memory
                             │ (cache.py)   │     single writer, many readers
                             └─────────────┘
                                    │
                        ┌───────────┼───────────┐
                        ▼           ▼           ▼
                  SSE Stream   Portfolio    Trade
                  /api/stream  Valuation    Execution
                  /prices      (future)     (future)
```

### Key Design Decisions

| Decision | Why |
|---|---|
| Single writer to cache | No locking contention; only one data source active at a time |
| `PriceCache` uses `threading.Lock` | SSE generator runs in async context but cache may be read from sync code (e.g., trade execution in a thread) |
| Monotonic `version` counter | SSE endpoint skips sending data if nothing changed — avoids redundant JSON serialization |
| `PriceUpdate` is frozen dataclass | Immutable snapshots are safe to pass across async boundaries without copying |
| Factory reads env var at call time | Testable — tests can set `MASSIVE_API_KEY` before calling the factory |

---

## 2. Module Layout

```
backend/app/market/
├── __init__.py          # Public exports (5 symbols)
├── models.py            # PriceUpdate dataclass
├── cache.py             # PriceCache (thread-safe in-memory store)
├── interface.py         # MarketDataSource ABC
├── simulator.py         # GBMSimulator + SimulatorDataSource
├── massive_client.py    # MassiveDataSource (Polygon.io REST)
├── factory.py           # Environment-driven factory function
├── seed_prices.py       # Default tickers, prices, GBM parameters
└── stream.py            # SSE streaming FastAPI router
```

Each module has a single responsibility. Circular imports are impossible because the dependency graph is a DAG:

```
models.py ← cache.py ← interface.py ← simulator.py
                                     ← massive_client.py
                        cache.py ← factory.py (imports simulator + massive)
                        cache.py ← stream.py
seed_prices.py ← simulator.py
```

---

## 3. PriceUpdate Model

**File:** `app/market/models.py`

An immutable snapshot of a single ticker's price at a point in time. This is the unit of data that flows through the entire system — written to the cache, read by SSE, serialized to JSON for the frontend.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
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
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
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

### Design Notes

- **`frozen=True`**: Prevents accidental mutation. Once created, a `PriceUpdate` is a read-only value object.
- **`slots=True`**: Memory-efficient — no `__dict__` per instance. Matters when we have many tickers with frequent updates.
- **`previous_price`**: Stored explicitly rather than computed from a history buffer. The cache provides the previous price when creating a new update.
- **`change` / `change_percent`**: Computed properties, not stored. They're derived from `price` and `previous_price`, so storing them would violate single source of truth.
- **`direction`**: A convenience for the frontend's flash animation logic. The frontend applies a green or red CSS class based on this value.
- **`to_dict()`**: Returns a plain dict suitable for `json.dumps()`. The SSE endpoint calls this for every ticker on every push.

### Usage Examples

```python
# Created by PriceCache.update() — not typically created directly
update = PriceUpdate(ticker="AAPL", price=191.50, previous_price=190.00)
assert update.change == 1.5
assert update.direction == "up"
assert update.to_dict()["change_percent"] == 0.7895

# Frozen — this raises dataclasses.FrozenInstanceError:
update.price = 200.0  # Error!
```

---

## 4. PriceCache

**File:** `app/market/cache.py`

The central integration point. One data source writes; SSE, portfolio, and trade endpoints read.

```python
class PriceCache:
    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0

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

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Design Notes

- **`threading.Lock`**, not `asyncio.Lock`: The cache is read by both async code (SSE generator) and potentially sync code (trade execution running in a thread via `asyncio.to_thread()`). A threading lock works in both contexts; an asyncio lock would not.
- **`version` counter**: Incremented on every `update()` call. The SSE endpoint compares `last_version` to `current_version` — if they match, it skips serialization. This is cheap (integer comparison) and avoids sending duplicate data when the data source hasn't ticked yet.
- **First update for a ticker**: When `prev` is `None`, `previous_price` is set to `price` itself, resulting in `direction="flat"` and `change=0`. This ensures the first SSE event for a new ticker shows flat rather than a misleading jump from zero.
- **Rounding**: `round(price, 2)` ensures prices are always cent-precise. The GBM simulator produces floating-point values with arbitrary precision; rounding here is the single normalization point.
- **`get_all()` returns a shallow copy**: Prevents the caller from mutating the internal dict. Since `PriceUpdate` is frozen, the values are safe to share.

### Integration with Other Components

```python
# Trade execution reads current price for fill:
price = cache.get_price("AAPL")
if price is None:
    raise ValueError("No price available for AAPL")
total_cost = price * quantity

# Portfolio valuation reads all prices:
all_prices = cache.get_all()
for ticker, update in all_prices.items():
    current_value = positions[ticker].quantity * update.price
```

---

## 5. MarketDataSource Interface

**File:** `app/market/interface.py`

The abstract contract that both implementations fulfill. Downstream code never imports the concrete classes directly — it receives a `MarketDataSource` from the factory.

```python
class MarketDataSource(ABC):
    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.
        Starts a background task that periodically writes to the PriceCache.
        Must be called exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.
        Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. Also removes from PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

### Lifecycle Contract

```
                  ┌──────────┐
                  │ Created  │  (factory returns unstarted source)
                  └────┬─────┘
                       │ await start(tickers)
                       ▼
                  ┌──────────┐
             ┌───▶│ Running  │◀───┐
             │    └────┬─────┘    │
  add_ticker │         │          │ remove_ticker
             │         │          │
             └─────────┘──────────┘
                       │
                       │ await stop()
                       ▼
                  ┌──────────┐
                  │ Stopped  │  (no more writes to cache)
                  └──────────┘
```

### Why All Methods Are Async

Even though `SimulatorDataSource.add_ticker()` is synchronous internally (it delegates to `GBMSimulator.add_ticker()`), the interface uses `async` uniformly because:

1. `MassiveDataSource.start()` does an immediate first poll (`await self._poll_once()`), which is genuinely async
2. A uniform interface means callers don't need to know which implementation they have
3. Calling `await` on a sync-completing coroutine has negligible overhead

---

## 6. GBM Simulator

**File:** `app/market/simulator.py`

Two classes in one file: the pure math engine (`GBMSimulator`) and its async wrapper (`SimulatorDataSource`).

### 6.1 GBMSimulator — The Math Engine

Generates correlated stock price movements using Geometric Brownian Motion with Cholesky decomposition for sector correlations.

```python
class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8

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
```

#### The GBM Formula

Each tick computes:

```
S(t+dt) = S(t) × exp((μ - σ²/2) × dt + σ × √dt × Z)
```

- **`μ` (mu)**: Annualized drift. 0.05 = 5% expected annual return.
- **`σ` (sigma)**: Annualized volatility. 0.25 = 25% annual standard deviation.
- **`dt`**: Time step as fraction of a trading year. For 500ms ticks: `0.5 / 5,896,800 ≈ 8.48×10⁻⁸`.
- **`Z`**: A correlated standard normal draw (from Cholesky decomposition).

The tiny `dt` means each tick produces sub-cent moves. Over thousands of ticks, this accumulates into realistic-looking price charts with proper statistical properties.

#### The `step()` Method — Hot Path

```python
def step(self) -> dict[str, float]:
    n = len(self._tickers)
    if n == 0:
        return {}

    # 1. Generate n independent standard normal draws
    z_independent = np.random.standard_normal(n)

    # 2. Apply Cholesky to get correlated draws
    if self._cholesky is not None:
        z_correlated = self._cholesky @ z_independent
    else:
        z_correlated = z_independent

    result: dict[str, float] = {}
    for i, ticker in enumerate(self._tickers):
        params = self._params[ticker]
        mu = params["mu"]
        sigma = params["sigma"]

        # 3. GBM formula
        drift = (mu - 0.5 * sigma**2) * self._dt
        diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
        self._prices[ticker] *= math.exp(drift + diffusion)

        # 4. Random event: ~0.1% chance per tick per ticker
        if random.random() < self._event_prob:
            shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
            self._prices[ticker] *= (1 + shock)

        result[ticker] = round(self._prices[ticker], 2)

    return result
```

**Performance**: With 10 tickers at 2 ticks/second, `step()` runs 2× per second. NumPy generates all random draws in one call (`standard_normal(n)`), and the Cholesky multiplication is a single matrix-vector product. The per-ticker loop uses stdlib `math.exp` (faster than `np.exp` for scalars). Total time per `step()` is well under 1ms.

#### Cholesky Decomposition for Correlations

Stocks in the same sector move together. The simulator models this by:

1. Building an N×N correlation matrix from sector groupings
2. Computing the Cholesky decomposition: `L = cholesky(C)`
3. Multiplying independent normals by `L` to produce correlated draws

```python
def _rebuild_cholesky(self) -> None:
    n = len(self._tickers)
    if n <= 1:
        self._cholesky = None
        return

    corr = np.eye(n)  # Diagonal = 1.0
    for i in range(n):
        for j in range(i + 1, n):
            rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
            corr[i, j] = rho
            corr[j, i] = rho

    self._cholesky = np.linalg.cholesky(corr)
```

The correlation values come from sector groupings:

| Pair Type | Correlation |
|---|---|
| Tech + Tech (AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX) | 0.6 |
| Finance + Finance (JPM, V) | 0.5 |
| TSLA + anything | 0.3 |
| Cross-sector / unknown | 0.3 |

The matrix is rebuilt whenever tickers are added/removed. This is O(n²) but n < 50, so it takes microseconds.

#### Dynamic Ticker Management

```python
def add_ticker(self, ticker: str) -> None:
    if ticker in self._prices:
        return
    self._add_ticker_internal(ticker)
    self._rebuild_cholesky()

def _add_ticker_internal(self, ticker: str) -> None:
    self._tickers.append(ticker)
    self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
    self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))
```

Dynamically added tickers not in `SEED_PRICES` get a random starting price between $50-$300 and default GBM parameters (σ=0.25, μ=0.05).

#### Random Events

For visual drama in the demo:

- Each tick, each ticker has a 0.1% chance of a sudden 2-5% shock
- With 10 tickers at 2 ticks/sec → expect an event roughly every 50 seconds
- Direction is 50/50 up or down

This creates the occasional dramatic spike that makes the demo visually engaging.

### 6.2 SimulatorDataSource — Async Wrapper

Implements `MarketDataSource` by running the `GBMSimulator` in a background `asyncio.Task`.

```python
class SimulatorDataSource(MarketDataSource):
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
        # Seed the cache so SSE has data immediately
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

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

#### Key Behaviors

- **Immediate cache seeding on `start()`**: When the SSE endpoint first connects, it needs data immediately. The `start()` method writes initial prices to the cache before starting the background loop.
- **Exception resilience**: If `step()` throws (e.g., numpy error), the loop logs the exception and continues. The simulator should never crash the entire application.
- **Clean cancellation**: `stop()` cancels the task and awaits it, catching `CancelledError`. Safe to call multiple times.
- **Named task**: `name="simulator-loop"` makes it easy to identify in asyncio debug output.

---

## 7. Massive API Client

**File:** `app/market/massive_client.py`

Real market data from the Massive (formerly Polygon.io) REST API. Used when `MASSIVE_API_KEY` is set.

```python
class MassiveDataSource(MarketDataSource):
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
        await self._poll_once()  # Immediate first poll
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None
```

### Polling Mechanism

```python
async def _poll_loop(self) -> None:
    while True:
        await asyncio.sleep(self._interval)
        await self._poll_once()

async def _poll_once(self) -> None:
    if not self._tickers or not self._client:
        return

    try:
        snapshots = await asyncio.to_thread(self._fetch_snapshots)
        for snap in snapshots:
            try:
                price = snap.last_trade.price
                timestamp = snap.last_trade.timestamp / 1000.0  # ms → seconds
                self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
            except (AttributeError, TypeError) as e:
                logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
    except Exception as e:
        logger.error("Massive poll failed: %s", e)

def _fetch_snapshots(self) -> list:
    return self._client.get_snapshot_all(
        market_type=SnapshotMarketType.STOCKS,
        tickers=self._tickers,
    )
```

### Design Notes

- **`asyncio.to_thread()`**: The `massive` Python client is synchronous. Running it in a thread prevents blocking the event loop while the HTTP request is in flight.
- **Single API call for all tickers**: The snapshot endpoint accepts a comma-separated list of tickers and returns all of them in one response. This is critical for staying within rate limits (free tier: 5 req/min).
- **15-second default interval**: At 4 polls/minute, this safely stays under the free tier limit of 5 req/min, leaving headroom for other API calls.
- **Timestamp conversion**: Massive returns Unix milliseconds; our cache uses Unix seconds. Division by 1000 happens here at the boundary.
- **Ticker normalization**: `add_ticker()` calls `.upper().strip()` to normalize input. This prevents duplicate entries from case mismatches.
- **Graceful error handling**: API failures (401, 429, network) are logged but don't crash the loop. The next poll interval will retry automatically.

### API Response Structure

The endpoint `GET /v2/snapshot/locale/us/markets/stocks/tickers` returns:

```json
{
  "tickers": [
    {
      "ticker": "AAPL",
      "lastTrade": {
        "p": 191.15,       // <-- we use this as the price
        "t": 1636573458000 // <-- Unix milliseconds
      },
      "todaysChange": 0.90,
      "todaysChangePerc": 0.47
    }
  ]
}
```

The `massive` Python client wraps this into objects: `snap.ticker`, `snap.last_trade.price`, `snap.last_trade.timestamp`.

---

## 8. Factory

**File:** `app/market/factory.py`

Simple environment-variable-driven factory. Returns an **unstarted** source — the caller must `await source.start(tickers)`.

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

### Why a Factory Function (Not a Class)

- No state to manage — it reads one env var and returns an object
- Easier to test than a class with an `__init__`
- Follows Python convention: factory functions are idiomatic for simple creation logic

### Testing the Factory

```python
def test_factory_returns_simulator_when_no_key(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)

def test_factory_returns_massive_when_key_set(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key-123")
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, MassiveDataSource)

def test_factory_ignores_whitespace_only_key(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "   ")
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)
```

---

## 9. SSE Streaming

**File:** `app/market/stream.py`

The SSE endpoint reads from `PriceCache` and pushes updates to connected browser clients via Server-Sent Events.

```python
def create_stream_router(price_cache: PriceCache) -> APIRouter:
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
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
```

### Event Generator

```python
async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"  # Auto-reconnect after 1 second

    last_version = -1

    try:
        while True:
            if await request.is_disconnected():
                break

            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()

                if prices:
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    payload = json.dumps(data)
                    yield f"data: {payload}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
```

### SSE Wire Format

Each event looks like this on the wire:

```
retry: 1000

data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.45,"timestamp":1710000000.0,"change":0.05,"change_percent":0.0263,"direction":"up"},"GOOGL":{...}}

data: {"AAPL":{"ticker":"AAPL","price":190.52,...},...}

```

### Design Notes

- **`retry: 1000`**: Sent once at the start. Tells the browser's `EventSource` to wait 1 second before reconnecting if the connection drops.
- **Version-based change detection**: The generator checks `price_cache.version` each tick. If it hasn't changed (no new prices since last push), it skips the serialization and yield. This saves CPU when the data source is slower than the SSE interval (e.g., Massive at 15s intervals).
- **`X-Accel-Buffering: no`**: Disables nginx proxy buffering. Without this, SSE events accumulate in nginx's buffer and arrive in batches rather than streaming.
- **`Cache-Control: no-cache`**: Prevents any intermediate proxy from caching the event stream.
- **Client disconnect detection**: `request.is_disconnected()` cleanly stops the generator when the browser navigates away or closes the tab.
- **500ms interval**: Matches the simulator's tick rate. The frontend sees updates at ~2Hz, which is fast enough for a live-feeling dashboard without overwhelming the browser.

### Frontend Integration

```typescript
// Frontend: EventSource connection
const source = new EventSource('/api/stream/prices');

source.onmessage = (event) => {
  const prices: Record<string, PriceUpdate> = JSON.parse(event.data);
  for (const [ticker, update] of Object.entries(prices)) {
    // Update watchlist display, trigger flash animation
    updateTickerPrice(ticker, update);
  }
};

source.onerror = () => {
  // EventSource auto-reconnects after `retry` ms (1000ms)
  setConnectionStatus('reconnecting');
};
```

---

## 10. Seed Data & Configuration

**File:** `app/market/seed_prices.py`

All configurable constants for the default tickers and simulator behavior.

```python
# Realistic starting prices for the default watchlist
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,  "GOOGL": 175.00, "MSFT": 420.00,
    "AMZN": 185.00,  "TSLA": 250.00,  "NVDA": 800.00,
    "META": 500.00,  "JPM": 195.00,   "V": 280.00,
    "NFLX": 600.00,
}

# Per-ticker GBM parameters (sigma = volatility, mu = drift)
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},  # High vol, low drift
    "NVDA":  {"sigma": 0.40, "mu": 0.08},  # High vol, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},  # Low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},  # Low vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Defaults for dynamically added tickers
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for Cholesky decomposition
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6
INTRA_FINANCE_CORR = 0.5
CROSS_GROUP_CORR = 0.3
TSLA_CORR = 0.3  # TSLA does its own thing
```

### Parameter Rationale

- **Volatility (sigma)**: Scaled to reflect real-world behavior. TSLA (0.50) and NVDA (0.40) swing more than JPM (0.18) and V (0.17). This creates a visually varied dashboard — some tickers are calm, others are wild.
- **Drift (mu)**: Slight positive bias (3-8%) makes prices generally trend upward over a long session, which feels natural. NVDA has the highest drift (0.08) reflecting its growth-stock character.
- **Correlations**: Tech stocks move together (0.6) more than cross-sector pairs (0.3). This creates realistic-looking sector rotations where all tech names move up or down together.

---

## 11. Package Exports

**File:** `app/market/__init__.py`

Clean public API — only five symbols exported:

```python
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

All downstream code should import from `app.market`, not from submodules:

```python
# Good
from app.market import PriceCache, create_market_data_source, create_stream_router

# Bad — exposes internal module structure
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource
```

---

## 12. FastAPI Integration

The market data subsystem integrates with the FastAPI application at startup/shutdown. Here's how the main app wires everything together:

```python
# backend/app/main.py (future — not yet implemented)
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.market import PriceCache, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: start/stop market data on server start/stop."""
    # --- Startup ---
    price_cache = PriceCache()

    # Create and start the market data source
    source = create_market_data_source(price_cache)

    # Initial tickers = watchlist union held positions (from DB)
    # For now, use the default 10 tickers
    initial_tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                       "NVDA", "META", "JPM", "V", "NFLX"]
    await source.start(initial_tickers)

    # Store references on app.state for access in route handlers
    app.state.price_cache = price_cache
    app.state.market_source = source

    # Mount the SSE streaming router
    stream_router = create_stream_router(price_cache)
    app.include_router(stream_router)

    yield  # App is running

    # --- Shutdown ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)

# Serve static frontend files (Next.js export)
# app.mount("/", StaticFiles(directory="static", html=True), name="static")
```

### Accessing Market Data from Other Routes

Route handlers that need market data (trade execution, portfolio valuation) access the shared instances via `app.state`:

```python
# Example: trade endpoint reading current price
@router.post("/api/portfolio/trade")
async def execute_trade(trade: TradeRequest, request: Request):
    cache: PriceCache = request.app.state.price_cache
    current_price = cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(400, f"No price data for {trade.ticker}")
    # ... execute trade at current_price ...
```

### Watchlist Changes Propagate to Data Source

When a user adds/removes a ticker from the watchlist, the route handler also updates the data source:

```python
@router.post("/api/watchlist")
async def add_to_watchlist(body: WatchlistAdd, request: Request):
    source: MarketDataSource = request.app.state.market_source
    await source.add_ticker(body.ticker)
    # ... also persist to SQLite ...

@router.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(ticker: str, request: Request):
    source: MarketDataSource = request.app.state.market_source
    await source.remove_ticker(ticker)
    # ... also remove from SQLite ...
    # NOTE: only remove from source if ticker is NOT held in positions
```

---

## 13. Testing Strategy

### Unit Tests by Module

All tests live in `backend/tests/market/`. Run with:

```bash
cd backend
uv run --extra dev pytest tests/market/ -v
```

#### `test_models.py` — PriceUpdate

- Creation with all fields
- `change` and `change_percent` computed correctly (positive, negative, zero)
- `direction` returns "up", "down", or "flat"
- `to_dict()` serialization includes all fields
- Frozen immutability (mutation raises `FrozenInstanceError`)
- Edge case: `previous_price == 0` returns `change_percent == 0.0`

#### `test_cache.py` — PriceCache

- `update()` creates `PriceUpdate` with correct `previous_price`
- First update for a ticker has `direction == "flat"`
- `get()` returns `None` for unknown tickers
- `get_all()` returns a snapshot (shallow copy)
- `remove()` deletes from cache
- `version` increments on every `update()` call
- `__len__` and `__contains__` work correctly
- Custom timestamps are preserved
- Prices are rounded to 2 decimal places

#### `test_simulator.py` — GBMSimulator

- `step()` returns prices for all tickers
- All prices remain positive (GBM guarantee)
- Initial prices match seed data
- Dynamic `add_ticker()` / `remove_ticker()` work
- Cholesky matrix is rebuilt on ticker changes
- Pairwise correlation logic: tech-tech, finance-finance, TSLA special case, cross-sector
- Price changes are small per tick (sub-dollar)
- Random events can produce larger moves

#### `test_simulator_source.py` — SimulatorDataSource (async)

- Cache is populated immediately on `start()`
- Prices update over time (wait for a few intervals)
- `stop()` is idempotent
- `add_ticker()` seeds cache immediately
- `remove_ticker()` removes from cache
- Empty ticker list is handled
- Exception in `step()` doesn't crash the loop
- Custom `update_interval` is respected

#### `test_massive.py` — MassiveDataSource (async, mocked)

- Cache is populated after poll
- Malformed snapshots (missing fields) are skipped gracefully
- API errors don't crash the poll loop
- Timestamps are converted from ms to seconds
- Tickers are normalized to uppercase
- `add_ticker()` / `remove_ticker()` modify the tracked set
- Empty ticker list skips the API call
- `stop()` is idempotent
- Immediate first poll on `start()`

#### `test_factory.py` — Factory

- Returns `SimulatorDataSource` when `MASSIVE_API_KEY` is unset/empty/whitespace
- Returns `MassiveDataSource` when `MASSIVE_API_KEY` has a value
- Passes the `PriceCache` to the created source
- Passes the API key to `MassiveDataSource`

### Mocking the Massive Client in Tests

Tests for `MassiveDataSource` mock the `RESTClient` to avoid real API calls:

```python
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

@pytest.fixture
def mock_snapshot():
    """Create a mock Massive API snapshot object."""
    snap = MagicMock()
    snap.ticker = "AAPL"
    snap.last_trade.price = 191.50
    snap.last_trade.timestamp = 1710000000000  # Unix ms
    return snap

@pytest.fixture
def mock_client(mock_snapshot):
    client = MagicMock()
    client.get_snapshot_all.return_value = [mock_snapshot]
    return client

async def test_massive_updates_cache(mock_client):
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache)

    with patch.object(source, '_fetch_snapshots', return_value=[mock_snapshot]):
        await source.start(["AAPL"])
        assert cache.get_price("AAPL") == 191.50
        await source.stop()
```

---

## 14. Error Handling & Resilience

### Simulator

| Failure | Handling |
|---|---|
| `step()` raises exception | Logged via `logger.exception()`, loop continues on next tick |
| `numpy` error in Cholesky | Should never happen (correlation matrix is always positive definite by construction) |
| `add_ticker()` with unknown ticker | Random price $50-300, default GBM params, logged |

### Massive API

| Failure | Handling |
|---|---|
| 401 Unauthorized | Logged as error, retried next interval (bad API key) |
| 429 Rate Limited | Logged as error, retried next interval |
| Network timeout | Logged as error, retried next interval |
| Malformed snapshot (missing `last_trade`) | Individual snapshot skipped, others still processed |
| Empty response | No cache updates, logged at debug level |

### SSE Streaming

| Failure | Handling |
|---|---|
| Client disconnects | Detected via `request.is_disconnected()`, generator exits cleanly |
| Generator cancelled | `CancelledError` caught, exits cleanly |
| No data in cache | No event sent (waits for first data) |
| JSON serialization error | Would propagate up — but `PriceUpdate.to_dict()` returns only basic types, so this shouldn't happen |

### Key Resilience Principle

**No component's failure should crash the application.** The data source loop catches all exceptions. The SSE generator handles disconnects and cancellation. The cache's thread lock prevents data races. The factory gracefully falls back to the simulator.
