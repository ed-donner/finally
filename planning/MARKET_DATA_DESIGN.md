# Market Data Backend — Design

Implementation-ready design for the FinAlly market data subsystem: a unified interface with two interchangeable implementations (GBM simulator and Massive/Polygon.io REST API), a thread-safe price cache, and an SSE endpoint that streams live prices to the frontend.

**Status:** Implemented in `backend/app/market/` (9 files, ~640 lines). 73 tests passing, 91% coverage. This document reflects the actual shipped code plus the integration points ("Section 10" onward) that the rest of the backend (FastAPI app, routes, DB) still needs to wire up.

---

## Table of Contents

1. [Architecture](#1-architecture)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model)
4. [Price Cache — `cache.py`](#4-price-cache)
5. [Abstract Interface — `interface.py`](#5-abstract-interface)
6. [Seed Prices & Ticker Parameters — `seed_prices.py`](#6-seed-prices--ticker-parameters)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client)
9. [Factory — `factory.py`](#9-factory)
10. [SSE Streaming Endpoint — `stream.py`](#10-sse-streaming-endpoint)
11. [FastAPI Lifecycle Integration (to be built)](#11-fastapi-lifecycle-integration-to-be-built)
12. [Watchlist Coordination](#12-watchlist-coordination)
13. [Testing Strategy](#13-testing-strategy)
14. [Error Handling & Edge Cases](#14-error-handling--edge-cases)
15. [Configuration Summary](#15-configuration-summary)

---

## 1. Architecture

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM simulator (default, no API key needed)
└── MassiveDataSource    →  Polygon.io REST poller (when MASSIVE_API_KEY is set)
        │
        ▼
   PriceCache (thread-safe, in-memory, single source of truth)
        │
        ├──→ SSE stream endpoint  (/api/stream/prices)
        ├──→ Portfolio valuation  (reads current price for P&L)
        └──→ Trade execution      (reads current price to fill an order)
```

Both data sources implement the same `MarketDataSource` ABC — a strategy pattern. Neither the SSE endpoint nor any other downstream code needs to know or care which one is active; everything reads from the shared `PriceCache`. This also means the update cadence of the source (simulator: 500ms: Massive: 15s) is decoupled from the cadence at which the SSE loop polls the cache.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py          # Re-exports the public API
      models.py             # PriceUpdate dataclass
      cache.py              # PriceCache (thread-safe in-memory store)
      interface.py          # MarketDataSource ABC
      seed_prices.py        # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, CORRELATION_GROUPS
      simulator.py           # GBMSimulator + SimulatorDataSource
      massive_client.py      # MassiveDataSource
      factory.py             # create_market_data_source()
      stream.py              # SSE endpoint (FastAPI router factory)
  tests/
    market/
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_factory.py
      test_massive.py
  market_data_demo.py        # Rich terminal demo (uv run market_data_demo.py)
```

Each file has a single responsibility. `app/market/__init__.py` re-exports the public API so the rest of the backend imports from `app.market` without reaching into submodules:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source, create_stream_router
```

---

## 3. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the only object that leaves the market data layer. SSE streaming, portfolio valuation, and trade execution all work exclusively with this type.

```python
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

**Design decisions**

- `frozen=True, slots=True` — immutable value objects, cheap to create (many are created per second), safe to share across async tasks without copying.
- `change`, `change_percent`, `direction` are computed properties derived from `price`/`previous_price` — they can never drift out of sync with the underlying values.
- `to_dict()` is the single serialization point used by the SSE endpoint (and, later, REST responses that embed a price).

---

## 4. Price Cache

**File: `backend/app/market/cache.py`**

The central data hub. Exactly one data source writes to it at a time; the SSE endpoint, portfolio valuation, and trade execution all read from it.

```python
class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker."""

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

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

    def __len__(self) -> int: ...
    def __contains__(self, ticker: str) -> bool: ...
```

**Why a version counter?** The SSE loop polls the cache every ~500ms. Without a version counter it would re-serialize and re-send every ticker on every tick even when nothing changed (the Massive source only updates every 15s). The counter lets the SSE loop skip a send when nothing is new — see [Section 10](#10-sse-streaming-endpoint).

**Why `threading.Lock` instead of `asyncio.Lock`?** The Massive client's synchronous `get_snapshot_all()` call runs inside `asyncio.to_thread()`, which executes in a real OS thread — an `asyncio.Lock` would not protect against that thread racing with the event loop. `threading.Lock` is safe from both a worker thread and the async event loop.

- First update for a ticker sets `previous_price == price`, so `direction` is `"flat"` (no false "up"/"down" flash on the very first tick).
- Prices are rounded to 2 decimal places once, in the cache — not scattered across producers.

---

## 5. Abstract Interface

**File: `backend/app/market/interface.py`**

```python
class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.

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

The source *pushes* updates into the cache rather than *returning* them. This decouples timing entirely: the simulator ticks at 500ms, Massive polls at 15s, but the SSE layer always reads at its own fixed cadence and never needs to know which source is active or how often it updates.

---

## 6. Seed Prices & Ticker Parameters

**File: `backend/app/market/seed_prices.py`**

Constants only — no logic, no imports beyond stdlib. Shared by the simulator (initial prices + GBM parameters) and read by the default watchlist.

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00, "TSLA": 250.00,
    "NVDA": 800.00, "META": 500.00, "JPM": 195.00, "V": 280.00, "NFLX": 600.00,
}

# sigma: annualized volatility (higher = more price movement)
# mu:    annualized drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Default parameters for tickers not in the list above (dynamically added)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6       # Tech stocks move together
INTRA_FINANCE_CORR = 0.5    # Finance stocks move together
CROSS_GROUP_CORR = 0.3      # Between sectors / unknown tickers
TSLA_CORR = 0.3             # TSLA does its own thing
```

A ticker added dynamically and not present in `SEED_PRICES`/`TICKER_PARAMS` gets a random seed price in `$50-$300` and `DEFAULT_PARAMS` — see `GBMSimulator._add_ticker_internal` below.

---

## 7. GBM Simulator

**File: `backend/app/market/simulator.py`**

Two classes: `GBMSimulator` (pure math engine, stateful) and `SimulatorDataSource` (the `MarketDataSource` implementation that wraps it in an async loop and writes to the cache).

### 7.1 The math

Geometric Brownian Motion — the same model underlying Black-Scholes — produces continuous, always-positive, log-normally-distributed price paths:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

`dt` is expressed as a fraction of a trading year so that `mu`/`sigma` can be given as familiar annualized numbers:

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800 (252 trading days, 6.5h/day)
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8, i.e. one 500ms tick
```

This tiny `dt` produces small, sub-cent-scale moves per tick that accumulate naturally into realistic intraday ranges over time.

### 7.2 Correlated moves via Cholesky decomposition

Real stocks don't move independently. Given a correlation matrix `C`, `L = cholesky(C)` transforms independent standard normals into correlated ones: `Z_correlated = L @ Z_independent`. Correlation is looked up per pair from the sector groups in `seed_prices.py`:

```python
@staticmethod
def _pairwise_correlation(t1: str, t2: str) -> float:
    tech = CORRELATION_GROUPS["tech"]
    finance = CORRELATION_GROUPS["finance"]

    if t1 == "TSLA" or t2 == "TSLA":
        return TSLA_CORR
    if t1 in tech and t2 in tech:
        return INTRA_TECH_CORR
    if t1 in finance and t2 in finance:
        return INTRA_FINANCE_CORR
    return CROSS_GROUP_CORR
```

The Cholesky matrix is rebuilt (`_rebuild_cholesky`) whenever a ticker is added or removed — O(n²), but n stays well under 50.

### 7.3 `GBMSimulator.step()` — the hot path

```python
def step(self) -> dict[str, float]:
    """Advance all tickers by one time step. Returns {ticker: new_price}."""
    n = len(self._tickers)
    if n == 0:
        return {}

    z_independent = np.random.standard_normal(n)
    z_correlated = self._cholesky @ z_independent if self._cholesky is not None else z_independent

    result: dict[str, float] = {}
    for i, ticker in enumerate(self._tickers):
        params = self._params[ticker]
        mu, sigma = params["mu"], params["sigma"]

        drift = (mu - 0.5 * sigma**2) * self._dt
        diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
        self._prices[ticker] *= math.exp(drift + diffusion)

        # Random event: ~0.1% chance per tick per ticker — a sudden 2-5% move for drama.
        # With 10 tickers at 2 ticks/sec, expect one roughly every 50 seconds.
        if random.random() < self._event_prob:
            shock_magnitude = random.uniform(0.02, 0.05)
            shock_sign = random.choice([-1, 1])
            self._prices[ticker] *= 1 + shock_magnitude * shock_sign

        result[ticker] = round(self._prices[ticker], 2)

    return result
```

`add_ticker` / `remove_ticker` / `get_price` / `get_tickers` round out the public surface; `_add_ticker_internal` seeds an unknown ticker with a random price in `$50-$300` and `DEFAULT_PARAMS`.

### 7.4 `SimulatorDataSource` — async wrapper

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5,
                 event_probability: float = 0.001) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed the cache immediately so SSE has data on its very first tick
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

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

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

**Key behaviors:** the cache is seeded *before* the loop starts (no blank-screen delay on first paint); `stop()` cancels and awaits the task, swallowing `CancelledError`, for clean shutdown under FastAPI's lifespan teardown; `_run_loop` catches exceptions per-tick so one bad step never kills the feed.

---

## 8. Massive API Client

**File: `backend/app/market/massive_client.py`**

Polls the Massive (formerly Polygon.io) REST snapshot endpoint on a configurable interval. The synchronous `massive` client runs inside `asyncio.to_thread()` so it never blocks the event loop.

### 8.1 The Massive Python SDK

```bash
uv add massive   # already a core dependency in pyproject.toml
```

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="...")  # or reads MASSIVE_API_KEY from env if omitted

# Single call returns snapshots for every requested ticker — this is what
# keeps us within the free tier's 5 req/min limit.
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT"],
)
for snap in snapshots:
    print(snap.ticker, snap.last_trade.price, snap.day.change_percent)
```

Relevant response fields per ticker: `last_trade.price` (current price, what we display/trade at), `last_trade.timestamp` (Unix **milliseconds**), `day.previous_close` / `day.change_percent` (day-over-day change, potential future use for a "day change %" column).

Rate limits: free tier 5 req/min → poll every 15s; paid tiers support 2-5s polling.

### 8.2 `MassiveDataSource`

```python
class MassiveDataSource(MarketDataSource):
    """Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call, then writes results to the PriceCache."""

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
        await self._poll_once()  # immediate first poll — no wait for the first interval
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

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)  # picked up on the next poll cycle

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

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
                    self._cache.update(
                        ticker=snap.ticker,
                        price=snap.last_trade.price,
                        timestamp=snap.last_trade.timestamp / 1000.0,  # ms -> seconds
                    )
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
        except Exception as e:
            logger.error("Massive poll failed: %s", e)  # retried on the next interval

    def _fetch_snapshots(self) -> list:
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS, tickers=self._tickers,
        )
```

**Error handling philosophy** — the poller is deliberately resilient, never crashes the background task:

| Error | Behavior |
|-------|----------|
| 401 Unauthorized (bad key) | Logged as error; poller keeps retrying every `poll_interval` |
| 429 Rate limited | Logged as error; retried on the next cycle |
| Network timeout | Logged as error; retried automatically |
| Malformed snapshot for one ticker | That ticker is skipped with a warning; the rest of the batch still updates |
| All tickers fail | Cache retains last-known prices — SSE keeps streaming stale-but-present data rather than nothing |

`RESTClient` and `SnapshotMarketType` are imported at module level (not lazily) because `massive` is a **core** dependency declared in `pyproject.toml` — every environment has it installed regardless of which data source ends up active.

---

## 9. Factory

**File: `backend/app/market/factory.py`**

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """MASSIVE_API_KEY set and non-empty -> MassiveDataSource; otherwise -> SimulatorDataSource.
    Returns an unstarted source — caller must await source.start(tickers)."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(["AAPL", "GOOGL", "MSFT", ...])
```

---

## 10. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

A FastAPI route that holds a long-lived `text/event-stream` connection open and pushes price updates as they land in the cache.

```python
router = APIRouter(prefix="/api/stream", tags=["streaming"])

def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Factory pattern: injects the PriceCache without module-level globals."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # disable nginx buffering if ever proxied
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache, request: Request, interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"  # browser EventSource auto-reconnect delay

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
                    yield f"data: {json.dumps(data)}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
```

**Wire format** — one JSON object keyed by ticker per event:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}
```

Frontend side:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
  const prices = JSON.parse(event.data);  // { AAPL: {...}, GOOGL: {...}, ... }
};
```

**Why poll-and-push instead of event-driven?** Fixed-interval polling of the cache (rather than a callback fired by the data source) gives predictable, evenly-spaced updates, which matters for the frontend's sparkline accumulation — irregular spacing would distort the chart. The version counter keeps this cheap: no new version, no serialize, no send.

---

## 11. FastAPI Lifecycle Integration (to be built)

The market data subsystem is complete and tested in isolation, but `backend/app/main.py` does not exist yet — this section is the integration contract for whoever builds the FastAPI app, database, and routes next.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.market import PriceCache, MarketDataSource, create_market_data_source, create_stream_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    source = create_market_data_source(price_cache)
    app.state.market_source = source

    initial_tickers = await load_watchlist_tickers()  # from SQLite; seeded defaults on first run
    await source.start(initial_tickers)

    app.include_router(create_stream_router(price_cache))

    yield  # app is running

    # --- SHUTDOWN ---
    await source.stop()

app = FastAPI(title="FinAlly", lifespan=lifespan)

def get_price_cache() -> PriceCache:
    return app.state.price_cache

def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

Other routes pull the cache/source via FastAPI dependency injection:

```python
@router.post("/portfolio/trade")
async def execute_trade(trade: TradeRequest, price_cache: PriceCache = Depends(get_price_cache)):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(404, f"No price available for {trade.ticker}")
    # ... validate cash/shares, execute at current_price ...

@router.post("/watchlist")
async def add_to_watchlist(payload: WatchlistAdd, source: MarketDataSource = Depends(get_market_source)):
    await db.insert_watchlist_entry(payload.ticker)
    await source.add_ticker(payload.ticker)
```

---

## 12. Watchlist Coordination

Adding/removing a ticker (via the REST API or the LLM chat's `watchlist_changes`) must update both the database and the live data source.

**Add:**
```
POST /api/watchlist {ticker: "PYPL"}
  -> INSERT into watchlist table
  -> await source.add_ticker("PYPL")
       Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
       Massive:   appends to the ticker list, appears on the next poll (<= 15s)
  -> 200 OK
```

**Remove — with an open-position guard:**
```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(ticker: str, source: MarketDataSource = Depends(get_market_source)):
    await db.delete_watchlist_entry(ticker)

    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)  # only stop tracking if nothing is held

    return {"status": "ok"}
```

If the user still holds shares of a ticker removed from the watchlist, the data source keeps tracking it so portfolio valuation stays accurate — it just no longer shows up in the watchlist UI.

---

## 13. Testing Strategy

**Current state: 73 tests, all passing, 91% overall coverage** (`uv run --extra dev pytest --cov=app`).

| Module | Coverage | Notes |
|---|---|---|
| `models.py` | 100% | |
| `cache.py` | 100% | |
| `interface.py` | 100% | ABC — trivially covered by subclass tests |
| `seed_prices.py` | 100% | Constants only |
| `factory.py` | 100% | |
| `simulator.py` | 98% | Uncovered: a duplicate-add guard and a logged exception path |
| `massive_client.py` | 94% | Uncovered: a couple of malformed-snapshot branches and the real (unmocked) `_fetch_snapshots` |
| `stream.py` | 33% | SSE generator requires a running ASGI server; see below |

### 13.1 Unit tests — `GBMSimulator`

```python
def test_prices_are_positive(self):
    """GBM prices can never go negative (exp() is always positive)."""
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        assert sim.step()["AAPL"] > 0

def test_add_ticker(self):
    sim = GBMSimulator(tickers=["AAPL"])
    sim.add_ticker("TSLA")
    assert "TSLA" in sim.step()

def test_cholesky_rebuilds_on_add(self):
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim._cholesky is None       # 1 ticker: no correlation matrix needed
    sim.add_ticker("GOOGL")
    assert sim._cholesky is not None   # 2 tickers: matrix now exists
```

### 13.2 Unit tests — `PriceCache`

```python
def test_direction_up(self):
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    update = cache.update("AAPL", 191.00)
    assert update.direction == "up"
    assert update.change == 1.00

def test_version_increments(self):
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.00)
    assert cache.version == v0 + 1
```

### 13.3 Integration tests — `SimulatorDataSource`

```python
async def test_start_populates_cache(self):
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
    await source.start(["AAPL", "GOOGL"])
    assert cache.get("AAPL") is not None  # seeded before the first loop tick
    await source.stop()
```

### 13.4 Mocked tests — `MassiveDataSource`

`massive` is a real dependency (not lazily imported), so `RESTClient` can be patched directly at the module path, and `_fetch_snapshots` can be patched on the instance to avoid any real network call:

```python
async def test_poll_updates_cache(self):
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    source._tickers = ["AAPL", "GOOGL"]
    source._client = MagicMock()  # satisfy the _poll_once guard

    mock_snapshots = [_make_snapshot("AAPL", 190.50, 1707580800000),
                      _make_snapshot("GOOGL", 175.25, 1707580800000)]

    with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.50

async def test_malformed_snapshot_skipped(self):
    """One bad snapshot doesn't block the rest of the batch."""
    ...
    bad_snap.last_trade = None  # triggers AttributeError, caught and logged
    ...
    assert cache.get_price("AAPL") == 190.50   # good ticker still updates
    assert cache.get_price("BAD") is None      # bad ticker skipped
```

### 13.5 SSE integration test (recommended, not yet written)

`stream.py` sits at 33% coverage because exercising the generator needs a running ASGI server. Once `main.py` exists, add an `httpx.AsyncClient(app=app, ...)` test that connects to `/api/stream/prices`, reads the first event, and asserts the payload shape and the `retry:` directive.

---

## 14. Error Handling & Edge Cases

**Empty watchlist at startup.** `start([])` is valid for both sources: the simulator produces no prices, Massive skips its API call. The SSE endpoint sends nothing until a ticker is added, at which point tracking begins immediately (simulator) or on the next poll (Massive).

**Trading a ticker with no cached price yet** (just added, Massive hasn't polled):
```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(400, f"Price not yet available for {ticker}. Please wait a moment and try again.")
```
The simulator avoids this in practice by seeding the cache synchronously inside `add_ticker()`; Massive may have a real gap of up to `poll_interval` seconds.

**Invalid `MASSIVE_API_KEY`.** First poll returns 401; the poller logs and keeps retrying forever rather than crashing. The frontend's connection indicator shows "connected" (the SSE stream itself is healthy) even though no prices arrive — the fix is to correct the key and restart, not something the backend can self-heal.

**Thread safety under load.** `PriceCache`'s `threading.Lock` guards a tiny critical section (dict get/set); at 10 tickers / 2 updates-per-second contention is negligible. If this project ever tracked hundreds of tickers with many concurrent SSE readers, a `ReadWriteLock` would be the next optimization — not needed here.

**Simulator numerical stability.** `exp(drift + diffusion)` is always positive, so prices can never go negative; rounding to 2 decimals happens once, inside `step()`.

---

## 15. Configuration Summary

| Parameter | Location | Default | Description |
|-----------|----------|---------|-------------|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set, use Massive API; otherwise use the simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5`s | Time between simulator ticks |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0`s | Time between Massive API polls (free tier: 5 req/min) |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of a random 2-5% shock per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `~8.5e-8` | GBM time step, as a fraction of a trading year |
| SSE push interval | `_generate_events()` | `0.5`s | Cache-poll cadence in the SSE loop |
| SSE retry directive | `_generate_events()` | `1000`ms | Browser `EventSource` reconnection delay |

### Demo

```bash
cd backend
uv run market_data_demo.py
```

A Rich terminal dashboard: all 10 default tickers live-updating with sparklines, color-coded direction arrows, and an event log for notable moves. Runs 60 seconds or until Ctrl+C — useful for eyeballing the simulator's behavior without standing up the full FastAPI app.
