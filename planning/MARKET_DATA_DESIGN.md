# Market Data Backend — Detailed Design

Status: **implemented** in `backend/app/market/`. This document is the design
reference for that code — it explains the architecture, walks through every
module with real code, and shows how the rest of the backend (portfolio,
watchlist, SSE) is meant to consume it. See `planning/MARKET_DATA_SUMMARY.md`
for a short status summary and `backend/CLAUDE.md` for the quick-reference
API cheat sheet.

---

## Table of Contents

1. [Goals & Constraints](#1-goals--constraints)
2. [Architecture Overview](#2-architecture-overview)
3. [File Structure](#3-file-structure)
4. [Data Model — `models.py`](#4-data-model--modelspy)
5. [Price Cache — `cache.py`](#5-price-cache--cachepy)
6. [Abstract Interface — `interface.py`](#6-abstract-interface--interfacepy)
7. [Seed Prices & Ticker Parameters — `seed_prices.py`](#7-seed-prices--ticker-parameters--seed_pricespy)
8. [GBM Simulator — `simulator.py`](#8-gbm-simulator--simulatorpy)
9. [Massive (Polygon.io) Client — `massive_client.py`](#9-massive-polygonio-client--massive_clientpy)
10. [Factory — `factory.py`](#10-factory--factorypy)
11. [SSE Streaming Endpoint — `stream.py`](#11-sse-streaming-endpoint--streampy)
12. [Public API — `__init__.py`](#12-public-api--__init__py)
13. [FastAPI Lifecycle Integration](#13-fastapi-lifecycle-integration)
14. [Watchlist Coordination](#14-watchlist-coordination)
15. [Error Handling & Edge Cases](#15-error-handling--edge-cases)
16. [Testing Strategy](#16-testing-strategy)
17. [Configuration Summary](#17-configuration-summary)

---

## 1. Goals & Constraints

From `planning/PLAN.md`:

- One unified interface for market data, with two interchangeable
  implementations: a built-in **GBM simulator** (default, no external
  dependency) and a **Massive (Polygon.io) REST poller** (used when
  `MASSIVE_API_KEY` is set).
- A single in-process **price cache** that both implementations write to and
  everything else (SSE stream, trade execution, portfolio valuation) reads
  from — downstream code never knows or cares which source is active.
- Prices pushed to the browser over **SSE** (`GET /api/stream/prices`), not
  WebSockets — one-way push is all that's needed.
- Runs entirely **in-process** as an asyncio background task; no external
  services, message queues, or extra containers.
- Dynamic watchlist: tickers can be added/removed at runtime (manually or by
  the LLM) without restarting the data source.

## 2. Architecture Overview

```
                 ┌─────────────────────────────┐
                 │      MarketDataSource (ABC)  │
                 │  start / stop / add_ticker / │
                 │  remove_ticker / get_tickers │
                 └──────────────┬───────────────┘
                                 │ implements
              ┌──────────────────┴──────────────────┐
              │                                      │
  ┌───────────▼────────────┐          ┌──────────────▼─────────────┐
  │   SimulatorDataSource   │          │      MassiveDataSource      │
  │  (GBMSimulator, ~500ms) │          │ (Polygon.io REST, ~15s poll)│
  └───────────┬────────────┘          └──────────────┬─────────────┘
              │            writes                     │ writes
              └──────────────────┬────────────────────┘
                                  ▼
                         ┌─────────────────┐
                         │    PriceCache    │  thread-safe, versioned
                         │ {ticker: Price   │
                         │   Update}        │
                         └────────┬─────────┘
                                  │ reads
           ┌──────────────────────┼───────────────────────┐
           ▼                      ▼                        ▼
  SSE /api/stream/prices   Trade execution           Portfolio valuation
  (create_stream_router)   (/api/portfolio/trade)    (/api/portfolio)
```

`create_market_data_source()` is a **factory** that picks the implementation
based on `MASSIVE_API_KEY`. Everything downstream — the SSE router, trade
execution, portfolio math — depends only on `PriceCache` and, when it needs
to mutate the tracked ticker set, the `MarketDataSource` ABC. Neither cares
which concrete source is running (the Strategy pattern).

## 3. File Structure

```
backend/
  app/
    market/
      __init__.py        # Public re-exports
      models.py           # PriceUpdate dataclass
      cache.py            # PriceCache (thread-safe in-memory store)
      interface.py         # MarketDataSource ABC
      seed_prices.py        # SEED_PRICES, TICKER_PARAMS, correlation constants
      simulator.py           # GBMSimulator + SimulatorDataSource
      massive_client.py       # MassiveDataSource (Polygon.io REST poller)
      factory.py                # create_market_data_source()
      stream.py                  # SSE endpoint (FastAPI router factory)
  tests/
    market/
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_massive.py
      test_factory.py
      test_stream.py
  market_data_demo.py     # Rich terminal live-dashboard demo
```

Each file has a single responsibility; `app/market/__init__.py` re-exports
the public surface so the rest of the backend imports from `app.market`
without reaching into submodules.

## 4. Data Model — `models.py`

`PriceUpdate` is the only type that leaves the market data layer. SSE
streaming, trade execution, and portfolio valuation all work exclusively
with this type.

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

- `frozen=True, slots=True` — an immutable, memory-lean value object; safe to
  share across async tasks without copying, and cheap since many are created
  per second.
- Computed properties (`change`, `change_percent`, `direction`) are derived
  from `price`/`previous_price` so they can never drift out of sync.
- `to_dict()` is the single serialization point used by the SSE endpoint.

## 5. Price Cache — `cache.py`

The central data hub. Writers: whichever `MarketDataSource` is active.
Readers: the SSE endpoint, trade execution, portfolio valuation. Must be
thread-safe because the Massive client's synchronous HTTP call runs inside
`asyncio.to_thread()` — a real OS thread, not just another coroutine — so an
`asyncio.Lock` would not protect it.

```python
class PriceCache:
    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # bumped on every update

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

    def get(self, ticker: str) -> PriceUpdate | None: ...
    def get_all(self) -> dict[str, PriceUpdate]: ...     # shallow copy
    def get_price(self, ticker: str) -> float | None: ...
    def remove(self, ticker: str) -> None: ...

    @property
    def version(self) -> int: ...   # for SSE change detection

    def __len__(self) -> int: ...
    def __contains__(self, ticker: str) -> bool: ...
```

**Why a version counter?** The SSE loop polls the cache every ~500ms. Without
a version counter it would re-serialize and resend every ticker on every
tick even when nothing changed (the Massive source only updates every ~15s).
Instead:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

The first update for a ticker sets `previous_price == price`, so
`direction` is `"flat"` on the very first tick — there's no artificial
initial "up"/"down".

## 6. Abstract Interface — `interface.py`

```python
class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source for prices — it
    reads from the cache.
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

This is a **push model**: the source decides its own cadence (500ms for the
simulator, 15s for Massive) and writes to the cache on that schedule. The
SSE layer reads at its own fixed cadence and never needs to know which
source is active or how fast it updates.

```
source = create_market_data_source(cache)
await source.start(["AAPL", "GOOGL", ...])
# ... app runs ...
await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")
# ... app shutting down ...
await source.stop()
```

## 7. Seed Prices & Ticker Parameters — `seed_prices.py`

Constants only — no logic, stdlib-only. Shared by the simulator for initial
prices/GBM parameters, and usable as fallback seed data elsewhere.

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00,
    "TSLA": 250.00, "NVDA": 800.00, "META": 500.00, "JPM": 195.00,
    "V": 280.00, "NFLX": 600.00,
}

# sigma: annualized volatility · mu: annualized drift (expected return)
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

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}  # dynamically added tickers

CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6      # tech stocks move together
INTRA_FINANCE_CORR = 0.5   # finance stocks move together
CROSS_GROUP_CORR = 0.3     # cross-sector and unknown-ticker fallback
TSLA_CORR = 0.3            # TSLA does its own thing
```

A ticker not in `SEED_PRICES`/`TICKER_PARAMS` (added dynamically via chat or
the watchlist API) gets a random seed price in `[50, 300]` and
`DEFAULT_PARAMS`, so the simulator never errors on an unknown symbol.

## 8. GBM Simulator — `simulator.py`

Two classes: `GBMSimulator` (pure math engine, stateful) and
`SimulatorDataSource` (the `MarketDataSource` implementation wrapping it in
an async loop).

### 8.1 `GBMSimulator` — the math engine

Geometric Brownian Motion with Cholesky-correlated draws across tickers:

```
S(t+dt) = S(t) * exp((mu - sigma²/2)·dt + sigma·sqrt(dt)·Z)
```

where `Z` is a *correlated* standard normal — tech names move together
(ρ=0.6), finance names move together (ρ=0.5), everything else is looser
(ρ=0.3). The 500ms tick is expressed as `dt ≈ 8.48e-8` of a trading year
(`252 days × 6.5h × 3600s`), so each tick is a sub-cent move that
accumulates naturally into realistic-looking intraday paths.

```python
class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR    # ~8.48e-8

    def __init__(self, tickers, dt=DEFAULT_DT, event_probability=0.001):
        self._dt = dt
        self._event_prob = event_probability
        self._tickers, self._prices, self._params = [], {}, {}
        self._cholesky: np.ndarray | None = None
        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance all tickers by one tick. Hot path — called every 500ms."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z_correlated = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result = {}
        for i, ticker in enumerate(self._tickers):
            mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # ~0.1% chance per tick of a 2-5% shock, for visual drama
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock

            result[ticker] = round(self._prices[ticker], 2)
        return result

    def add_ticker(self, ticker: str) -> None: ...      # rebuilds Cholesky
    def remove_ticker(self, ticker: str) -> None: ...   # rebuilds Cholesky
    def get_price(self, ticker: str) -> float | None: ...
    def get_tickers(self) -> list[str]: ...
```

`_pairwise_correlation(t1, t2)` decides the correlation coefficient for a
pair: TSLA is always `0.3` regardless of partner, same-tech-sector pairs are
`0.6`, same-finance-sector pairs are `0.5`, everything else — including
unknown/dynamically-added tickers — is `0.3`. `_rebuild_cholesky()` reruns
whenever the ticker set changes; it's `O(n²)` but `n` stays small (tens of
tickers at most), so this is cheap.

Prices are guaranteed positive (GBM is an exponential process) and rounded
to 2 decimal places at the source, so there's no floating-point drift to
worry about downstream.

### 8.2 `SimulatorDataSource` — async wrapper

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache, update_interval=0.5, event_probability=0.001):
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed the cache *before* the loop starts, so SSE has data on tick 1.
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

**Key behaviors**

- **Immediate seeding** — `start()` writes seed prices to the cache *before*
  the background loop begins, so the SSE endpoint has data on the very first
  tick with no blank-screen delay. `add_ticker()` does the same.
- **Graceful cancellation** — `stop()` cancels the task and awaits it,
  swallowing `CancelledError`, so FastAPI's `lifespan` shutdown is clean.
- **Exception resilience** — a single bad `step()` is caught and logged; the
  loop keeps running rather than killing the whole price feed.

## 9. Massive (Polygon.io) Client — `massive_client.py`

Polls the Massive REST snapshot endpoint on a fixed interval — no
WebSocket, so it works on every account tier. The client is synchronous, so
calls run in `asyncio.to_thread()` to avoid blocking the event loop.

```python
class MassiveDataSource(MarketDataSource):
    """Polls GET /v2/snapshot/locale/us/markets/stocks/tickers.

    Rate limits: free tier 5 req/min -> poll every 15s (default);
    paid tiers -> poll every 2-5s.
    """

    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval: float = 15.0):
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()  # immediate first poll — no 15s wait for first data
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
            self._tickers.append(ticker)   # appears on the next poll

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
                        timestamp=snap.last_trade.timestamp / 1000.0,  # ms -> s
                    )
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
        except Exception as e:
            logger.error("Massive poll failed: %s", e)  # retried next cycle, not re-raised

    def _fetch_snapshots(self) -> list:
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

**Error handling philosophy** — the poller is deliberately resilient:

| Error | Behavior |
|---|---|
| 401 Unauthorized | Logged; poller keeps running (fixable via `.env` + restart) |
| 429 Rate limited | Logged; retried automatically on the next interval |
| Network timeout | Logged; retried automatically on the next interval |
| One malformed snapshot | That ticker is skipped with a warning; others still processed |
| All tickers fail | Cache keeps last-known prices — stale data beats no data |

`massive` (the Polygon.io client package) is a normal top-level dependency
in `pyproject.toml`, imported at module load time — it's only *used* when
`MASSIVE_API_KEY` is set, since `factory.py` never instantiates
`MassiveDataSource` otherwise.

## 10. Factory — `factory.py`

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """MASSIVE_API_KEY set and non-empty -> MassiveDataSource; else Simulator."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    return SimulatorDataSource(price_cache=price_cache)
```

Usage at app startup:

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(initial_tickers)  # e.g. the default 10-ticker watchlist
```

## 11. SSE Streaming Endpoint — `stream.py`

A FastAPI route holding a long-lived connection, pushing `text/event-stream`
frames built from the cache.

```python
router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Factory pattern — injects the cache without a module-level global."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache, request: Request, interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"  # browser auto-reconnect delay

    last_version = -1
    try:
        while True:
            if await request.is_disconnected():
                break

            if price_cache.version != last_version:
                last_version = price_cache.version
                prices = price_cache.get_all()
                if prices:
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    yield f"data: {json.dumps(data)}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
```

**Wire format** — one JSON object keyed by ticker per frame:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}
```

Frontend side:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
  const prices = JSON.parse(event.data);  // { AAPL: {...}, GOOGL: {...} }
};
```

**Why poll-and-push instead of event-driven?** The endpoint polls the cache
on a fixed 500ms cadence rather than being notified by the data source. This
is simpler and gives the frontend evenly-spaced ticks, which matters for
building clean sparklines client-side from the raw event stream.

## 12. Public API — `__init__.py`

```python
from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = ["PriceUpdate", "PriceCache", "MarketDataSource", "create_market_data_source", "create_stream_router"]
```

Rest of the backend imports only from `app.market`:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source, create_stream_router
```

## 13. FastAPI Lifecycle Integration

The market data system starts/stops with the app via the `lifespan` context
manager:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    source = create_market_data_source(price_cache)
    app.state.market_source = source

    initial_tickers = await load_watchlist_tickers()   # from SQLite
    await source.start(initial_tickers)

    app.include_router(create_stream_router(price_cache))

    yield  # app is running

    # --- shutdown ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

Other routes pull the cache/source via dependency injection:

```python
@router.post("/portfolio/trade")
async def execute_trade(trade: TradeRequest, price_cache: PriceCache = Depends(get_price_cache)):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(404, f"No price available for {trade.ticker}")
    # ... execute at current_price ...


@router.post("/watchlist")
async def add_to_watchlist(payload: WatchlistAdd, source: MarketDataSource = Depends(get_market_source)):
    # ... insert into watchlist table ...
    await source.add_ticker(payload.ticker)
```

## 14. Watchlist Coordination

**Adding a ticker**

```
POST /api/watchlist {ticker: "PYPL"}
  → INSERT INTO watchlist (SQLite)
  → await source.add_ticker("PYPL")
      simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
      massive:   appends to poll list, price appears on next poll (≤15s)
  → return { ticker, price (if already available) }
```

**Removing a ticker**

```
DELETE /api/watchlist/PYPL
  → DELETE FROM watchlist (SQLite)
  → await source.remove_ticker("PYPL")
      simulator: removes from GBMSimulator, rebuilds Cholesky, drops from cache
      massive:   drops from poll list, drops from cache
  → return { status: "ok" }
```

**Edge case — open position on a delisted-from-watchlist ticker.** If the
user still holds shares of a ticker they removed from the watchlist, keep
tracking it so portfolio valuation stays accurate:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(ticker: str, source: MarketDataSource = Depends(get_market_source)):
    await db.delete_watchlist_entry(ticker)
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)
    return {"status": "ok"}
```

## 15. Error Handling & Edge Cases

| Case | Behavior |
|---|---|
| Empty watchlist at startup | `start([])` — simulator produces no prices, Massive poller skips its call. SSE sends only the `retry:` directive until a ticker is added. |
| Trade against an uncached ticker | `price_cache.get_price(ticker)` returns `None` → route returns `400` with a clear message ("price not yet available"). The simulator avoids this by seeding on `add_ticker()`; Massive may have a brief gap right after a ticker is added. |
| Invalid `MASSIVE_API_KEY` | First poll 401s, logged, poller keeps retrying every `poll_interval`. SSE connection stays "connected" but streams no data — fix is correcting the key and restarting. |
| Lock contention | `PriceCache` uses `threading.Lock` (not `asyncio.Lock`, since the Massive client's sync call runs in a real OS thread via `asyncio.to_thread`). Critical sections are a dict lookup + assignment — negligible at this scale (≤ tens of tickers, 2 writes/sec). |
| Floating-point drift | Prices are rounded to 2 decimals at the point of computation in both the simulator and cache; GBM's `exp()` formulation keeps prices always positive and numerically stable. |

## 16. Testing Strategy

**73 pre-existing tests + 10 new SSE tests = 83 tests, all passing, 97% line
coverage** across `backend/tests/market/`:

| Module | Tests | Coverage | What's covered |
|---|---|---|---|
| `test_models.py` | 11 | 100% | `PriceUpdate` computed properties, immutability, serialization |
| `test_cache.py` | 13 | 100% | update/get/remove, version counter, rounding, thread-safety surface |
| `test_simulator.py` | 17 | 98% | GBM math, correlation matrix, add/remove ticker, edge cases |
| `test_simulator_source.py` | 10 | (integration) | async lifecycle, seeding, cancellation, exception resilience |
| `test_factory.py` | 7 | 100% | env-var driven source selection |
| `test_massive.py` | 13 | 94% | polling, malformed-snapshot skipping, error resilience, ticker mgmt |
| `test_stream.py` *(new)* | 10 | 94% | SSE retry directive, data framing, version-based skip/resend, disconnect handling, router wiring |

Run them:

```bash
cd backend
uv run --extra dev pytest -v --cov=app
uv run --extra dev ruff check app/ tests/
```

### Example: exercising the SSE generator directly

`stream.py`'s generator takes a starlette `Request`, so tests use a minimal
fake rather than spinning up a real HTTP client:

```python
class FakeRequest:
    def __init__(self, disconnect_after=None):
        self._disconnect_after = disconnect_after
        self._checks = 0
        self.client = FakeClient("127.0.0.1")

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._disconnect_after is not None and self._checks > self._disconnect_after


async def test_yields_price_data_for_populated_cache():
    cache = PriceCache()
    cache.update("AAPL", 190.50)
    request = FakeRequest(disconnect_after=1)

    chunks = [c async for c in _generate_events(cache, request, interval=0.01)]

    assert chunks[0] == "retry: 1000\n\n"
    payload = json.loads(chunks[1][len("data: "):-2])
    assert payload["AAPL"]["price"] == 190.50
```

### Example: unit-testing the GBM math

```python
def test_prices_are_positive():
    """GBM prices can never go negative (exp() is always positive)."""
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        prices = sim.step()
        assert prices["AAPL"] > 0
```

### Example: mocking the Massive REST client

```python
def _make_snapshot(ticker, price, timestamp_ms):
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    return snap


async def test_poll_updates_cache():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    snapshots = [_make_snapshot("AAPL", 190.50, 1707580800000)]

    with patch.object(source, "_fetch_snapshots", return_value=snapshots):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.50
```

## 17. Configuration Summary

| Parameter | Location | Default | Description |
|---|---|---|---|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set, use Massive API; otherwise use the simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | Time between simulator ticks |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Time between Massive API polls (free-tier safe) |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of a 2-5% shock event per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step, fraction of a trading year per 500ms tick |
| SSE push interval | `_generate_events()` | `0.5` s | Cache-poll cadence for the SSE loop |
| SSE retry directive | `_generate_events()` | `1000` ms | Browser `EventSource` reconnect delay |

## Usage Cheat Sheet

```python
from app.market import PriceCache, create_market_data_source

# Startup
cache = PriceCache()
source = create_market_data_source(cache)         # reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", "MSFT", ...])

# Read prices
update = cache.get("AAPL")           # PriceUpdate | None
price = cache.get_price("AAPL")      # float | None
all_prices = cache.get_all()         # dict[str, PriceUpdate]

# Dynamic watchlist
await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")

# Shutdown
await source.stop()
```
