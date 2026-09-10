# Market Data Backend — Design

Detailed design for the market data subsystem: the unified provider interface, the GBM simulator, and the Massive (Polygon.io) REST client. This document reflects the actual implementation in `backend/app/market/` (see `planning/MARKET_DATA_SUMMARY.md` for the build/test status and `planning/archive/` for the original design drafts and code review).

## 1. Goals & Constraints

- One abstract interface (`MarketDataSource`) with two interchangeable implementations: a built-in simulator (default, zero config) and a Massive API poller (real data, opt-in via `MASSIVE_API_KEY`).
- Downstream code (SSE stream, portfolio valuation, trade execution) never talks to a data source directly — it only reads from a shared `PriceCache`. This means the frontend, the trade endpoint, and the LLM tool-calling layer are completely agnostic to where prices come from.
- Both implementations run as an in-process `asyncio` background task started once at app startup and stopped once at shutdown.
- Watchlist changes (add/remove ticker) must take effect without restarting the background task.

```
                     create_market_data_source(cache)
                              │
                 ┌────────────┴─────────────┐
                 │                           │
        SimulatorDataSource          MassiveDataSource
        (GBM, no API key)         (Polygon.io REST poll)
                 │                           │
                 └─────────────┬─────────────┘
                                ▼
                          PriceCache
                     (thread-safe, in-memory,
                      version counter)
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
    SSE /api/stream/prices  Portfolio valuation  Trade execution
      (EventSource push)    (P&L, snapshots)    (fill at cache price)
```

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py          # Public exports
      models.py            # PriceUpdate dataclass
      interface.py         # MarketDataSource ABC
      cache.py              # PriceCache
      seed_prices.py         # SEED_PRICES, TICKER_PARAMS, correlation constants
      simulator.py           # GBMSimulator + SimulatorDataSource
      massive_client.py      # MassiveDataSource
      factory.py              # create_market_data_source()
      stream.py                # create_stream_router() — SSE endpoint
  tests/
    market/
      test_models.py, test_cache.py, test_simulator.py,
      test_simulator_source.py, test_factory.py, test_massive.py
  market_data_demo.py       # Rich terminal demo (manual smoke test)
```

`app/market/__init__.py` is the only import surface the rest of the backend should use:

```python
from app.market import (
    PriceUpdate,
    PriceCache,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
)
```

## 3. Data Model — `models.py`

`PriceUpdate` is the single data structure that leaves the market data layer. It is frozen and slotted (immutable, memory-efficient), and derives `change`, `change_percent`, and `direction` as properties rather than storing them — this guarantees they're always consistent with `price`/`previous_price` and keeps the cache's write path (`PriceCache.update`) simple.

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

`to_dict()` is what actually goes over the wire on `/api/stream/prices` — the frontend reads `price`, `change_percent`, and `direction` directly to drive the flash animation and sparkline.

## 4. Abstract Interface — `interface.py`

Both data sources implement this ABC. It intentionally does **not** return prices — implementations push into the shared cache on their own schedule, so the interface is only about lifecycle and ticker set management.

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

        Starts a background task. Must be called exactly once.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Also removes it from the PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

**Lifecycle contract**, used identically regardless of which implementation is behind it:

```python
source = create_market_data_source(cache)
await source.start(["AAPL", "GOOGL", "MSFT", ...])   # once, at app startup
...
await source.add_ticker("TSLA")                        # on POST /api/watchlist
await source.remove_ticker("GOOGL")                     # on DELETE /api/watchlist/{ticker}
...
await source.stop()                                     # once, at app shutdown
```

## 5. Price Cache — `cache.py`

The single point of truth between producers (one data source at a time) and consumers (SSE stream, portfolio valuation, trade execution). Thread-safe because `MassiveDataSource` calls the synchronous Massive client via `asyncio.to_thread`, so writes can technically happen off the event loop thread.

```python
import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker."""

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Bumped on every update; drives SSE change detection

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price  # first update: flat

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
            return dict(self._prices)  # shallow copy — safe to iterate outside the lock

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

Design notes:
- **`version` is the SSE change-detection mechanism.** The stream endpoint polls `cache.version` every 500ms and only serializes+sends when it has changed since the last send, avoiding redundant payloads when the market is momentarily quiet (or between Massive polls).
- **First update for a ticker is always `direction="flat"`** (`previous_price == price`), which avoids a spurious green/red flash the instant a ticker is added to the watchlist.
- Memory is bounded at O(active tickers) — only the latest price per ticker is kept, no history.

## 6. Simulator — `simulator.py` + `seed_prices.py`

### 6.1 Why GBM

Geometric Brownian Motion is the standard continuous-time model for asset prices (it underlies Black-Scholes): prices are always positive (multiplicative noise via `exp`), and log-returns are normally distributed, which matches the qualitative behavior of real markets closely enough for a trading-terminal demo.

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

- `S(t)` — current price
- `mu` — annualized drift (expected return)
- `sigma` — annualized volatility
- `dt` — time step as a fraction of a trading year
- `Z` — standard normal random draw (correlated across tickers, see below)

At ~500ms per tick, `dt` is tiny:

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800 (252 trading days, 6.5h/day)
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ≈ 8.48e-8
```

This produces small, sub-cent moves per tick that compound into realistic intraday ranges over minutes of runtime.

### 6.2 Seed data — `seed_prices.py`

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00,
    "TSLA": 250.00, "NVDA": 800.00, "META": 500.00, "JPM": 195.00,
    "V": 280.00, "NFLX": 600.00,
}

TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05},
    "AMZN": {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03},   # high volatility
    "NVDA": {"sigma": 0.40, "mu": 0.08},   # high volatility, strong drift
    "META": {"sigma": 0.30, "mu": 0.05},
    "JPM": {"sigma": 0.18, "mu": 0.04},    # low volatility (bank)
    "V": {"sigma": 0.17, "mu": 0.04},      # low volatility (payments)
    "NFLX": {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}
INTRA_TECH_CORR = 0.6
INTRA_FINANCE_CORR = 0.5
CROSS_GROUP_CORR = 0.3
TSLA_CORR = 0.3   # TSLA does its own thing, even though it's in the "tech" set
```

Tickers not in `SEED_PRICES` (added dynamically by the user or the LLM) start at a random price in `[50, 300]` and use `DEFAULT_PARAMS`.

### 6.3 Correlated moves via Cholesky decomposition

Real stocks don't move independently — tech names tend to move together. Given an `n x n` correlation matrix `C`, its Cholesky factor `L` (`C = L @ L.T`) lets us turn `n` independent standard normals into `n` correlated ones: `Z_correlated = L @ Z_independent`.

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

def _rebuild_cholesky(self) -> None:
    n = len(self._tickers)
    if n <= 1:
        self._cholesky = None
        return
    corr = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
            corr[i, j] = corr[j, i] = rho
    self._cholesky = np.linalg.cholesky(corr)
```

The matrix built this way (identity diagonal, symmetric off-diagonal entries in `{0.3, 0.5, 0.6}`) is guaranteed positive semi-definite, so `np.linalg.cholesky` never fails. Rebuilt whenever a ticker is added or removed — O(n²), trivial for n < 50.

### 6.4 Random shock events

Every tick, every ticker has a small independent chance of a sudden 2–5% move, purely for visual drama on the dashboard:

```python
if random.random() < self._event_prob:      # default 0.001 (0.1%)
    shock_magnitude = random.uniform(0.02, 0.05)
    shock_sign = random.choice([-1, 1])
    self._prices[ticker] *= 1 + shock_magnitude * shock_sign
```

With 10 tickers at 2 ticks/sec, expect a visible shock roughly every ~50 seconds.

### 6.5 `GBMSimulator.step()` — the hot path

Called every 500ms by `SimulatorDataSource`; must stay fast.

```python
def step(self) -> dict[str, float]:
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

        if random.random() < self._event_prob:
            shock_magnitude = random.uniform(0.02, 0.05)
            shock_sign = random.choice([-1, 1])
            self._prices[ticker] *= 1 + shock_magnitude * shock_sign

        result[ticker] = round(self._prices[ticker], 2)

    return result
```

`add_ticker` / `remove_ticker` mutate `self._tickers`/`self._prices`/`self._params` and call `_rebuild_cholesky()`. `get_price()` and `get_tickers()` are the only public read accessors — callers should never reach into `_prices`/`_tickers` directly.

### 6.6 `SimulatorDataSource` — the async wrapper

Adapts `GBMSimulator` to the `MarketDataSource` interface by running `step()` in an `asyncio` background task.

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
        # Seed the cache immediately so SSE has data on the very first poll
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

`_run_loop` swallows and logs exceptions rather than letting the background task die — a long-running task must survive transient errors indefinitely.

## 7. Massive API Client — `massive_client.py`

### 7.1 Package & auth

- Python package: `massive` (`uv add massive`), min Python 3.9+.
- `RESTClient(api_key=...)` handles the `Authorization: Bearer <key>` header automatically.
- Base URL `https://api.massive.com` (legacy `https://api.polygon.io` still works).

### 7.2 Rate limits drive the poll interval

| Tier | Limit | Poll interval used |
|------|-------|---------------------|
| Free | 5 req/min | 15s (default) |
| Paid | much higher | 2–5s |

Polling the **snapshot-all** endpoint means one API call covers every watched ticker regardless of watchlist size — critical for staying under the free-tier 5 req/min cap.

### 7.3 The endpoint

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="...")
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT"],
)
for snap in snapshots:
    price = snap.last_trade.price               # what we use for the cache
    timestamp_ms = snap.last_trade.timestamp     # Unix milliseconds
```

Relevant response fields (see `planning/archive/MASSIVE_API.md` for the full schema): `last_trade.price`, `last_trade.timestamp` (ms), `day.previous_close`, `day.change_percent`.

### 7.4 `MassiveDataSource`

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
        await self._poll_once()   # immediate first poll — cache has data right away
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
            self._tickers.append(ticker)   # appears on the next scheduled poll

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
            # RESTClient is synchronous — offload to a thread so it never
            # blocks the event loop.
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            for snap in snapshots:
                try:
                    self._cache.update(
                        ticker=snap.ticker,
                        price=snap.last_trade.price,
                        timestamp=snap.last_trade.timestamp / 1000.0,  # ms -> s
                    )
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping malformed snapshot for %s: %s",
                                   getattr(snap, "ticker", "???"), e)
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Swallowed deliberately — the loop retries on the next interval.
            # Common causes: 401 (bad key), 429 (rate limit), transient network errors.

    def _fetch_snapshots(self) -> list:
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

Design notes:
- **`asyncio.to_thread`** is required because the `massive` SDK is synchronous (`requests`-based); calling it directly from a coroutine would block the whole event loop — including the SSE stream and every other request — for the duration of the HTTP round-trip.
- **Per-snapshot error isolation**: a single malformed snapshot (missing `last_trade`, etc.) is logged and skipped rather than aborting the whole poll cycle.
- **Poll-level error isolation**: any exception from the HTTP call itself (auth failure, rate limit, network) is caught, logged, and the loop simply waits for the next interval — a transient Massive outage never crashes the background task or the app.
- **Ticker add/remove is optimistic and cheap** — no API call is made immediately; the change is picked up on the next scheduled (or next explicit) poll. This matches the free-tier rate limit constraint (we can't afford an extra call per watchlist edit).

## 8. Factory — `factory.py`

Selects the implementation purely from the environment, at startup:

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """MASSIVE_API_KEY set and non-empty -> MassiveDataSource. Otherwise -> SimulatorDataSource."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        return SimulatorDataSource(price_cache=price_cache)
```

Everything downstream of this call site — SSE stream, portfolio code, trade execution, the LLM's trade-execution tool — only ever sees a `MarketDataSource` and a `PriceCache`. Swapping providers requires no other code change, which is the entire point of the strategy-pattern design.

## 9. SSE Streaming — `stream.py`

The only consumer that reads the cache on a fixed clock (everyone else reads on-demand: portfolio valuation reads `cache.get_price(ticker)` when computing P&L, trade execution reads it to fill an order).

```python
def create_stream_router(price_cache: PriceCache) -> APIRouter:
    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",   # disable nginx response buffering if proxied
            },
        )
    return router


async def _generate_events(price_cache: PriceCache, request: Request,
                            interval: float = 0.5) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"   # browser auto-reconnect delay if the connection drops

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

- **Version-gated sends**: nothing is written to the socket unless `PriceCache.version` changed since the last check — avoids sending an identical payload every 500ms when, e.g., the Massive poller hasn't ticked yet (its interval is 15–30x longer than the SSE poll loop).
- **Explicit disconnect detection** (`request.is_disconnected()`) so a client that closed the tab doesn't leave an orphaned generator running forever.
- **`retry: 1000`** is the SSE protocol's built-in reconnect hint — `EventSource` on the frontend needs no custom reconnection logic.
- One payload contains **every** tracked ticker (not per-ticker events) — simpler client-side state management, and the payload is small (10–50 tickers × a few floats).

## 10. Wiring It Together at App Startup

```python
from app.market import PriceCache, create_market_data_source, create_stream_router

price_cache = PriceCache()
market_source = create_market_data_source(price_cache)

@app.on_event("startup")
async def startup() -> None:
    tickers = load_watchlist_tickers()   # from SQLite, default seed on first run
    await market_source.start(tickers)

@app.on_event("shutdown")
async def shutdown() -> None:
    await market_source.stop()

app.include_router(create_stream_router(price_cache))
```

Watchlist mutation endpoints call `market_source.add_ticker()` / `remove_ticker()` in addition to their SQLite writes, so the price cache and the persisted watchlist never drift out of sync:

```python
@app.post("/api/watchlist")
async def add_to_watchlist(body: AddTickerRequest):
    ticker = body.ticker.upper().strip()
    db.insert_watchlist_ticker(ticker)          # persist
    await market_source.add_ticker(ticker)      # start streaming it
    return {"ticker": ticker}

@app.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(ticker: str):
    db.delete_watchlist_ticker(ticker)
    await market_source.remove_ticker(ticker)   # also evicts from PriceCache
    return {"ok": True}
```

Trade execution and portfolio valuation both read synchronously from the cache — no `await`, no network call, regardless of which `MarketDataSource` is active:

```python
def execute_trade(ticker: str, side: str, quantity: float, price_cache: PriceCache):
    price = price_cache.get_price(ticker)
    if price is None:
        raise TickerNotTrackedError(ticker)
    # ... validate cash/shares, write trades row, update positions row using `price` ...
```

## 11. Testing Strategy

Already implemented (see `planning/MARKET_DATA_SUMMARY.md` for current pass/coverage numbers):

- **`test_models.py`** — `change`/`change_percent`/`direction` correctness, `to_dict()` shape, zero-previous-price edge case.
- **`test_cache.py`** — first-update-is-flat, version increments, `get_all()` returns an independent copy, `remove()`.
- **`test_simulator.py`** — GBM math on a single ticker (mean/variance sanity over many steps), Cholesky matrix is valid for the full 10-ticker default set, shock events fire at roughly the configured probability, add/remove ticker rebuilds correlations correctly.
- **`test_simulator_source.py`** — `SimulatorDataSource` start/stop lifecycle, cache is seeded immediately on `start()`, task is actually cancelled on `stop()`.
- **`test_factory.py`** — env var present/absent/empty-string selects the right class.
- **`test_massive.py`** — snapshot parsing (price + ms→s timestamp conversion), malformed-snapshot skipping, poll-loop cancellation. Requires the `massive` package installed (it's a core dependency, not optional) since `RESTClient`/`SnapshotMarketType` are imported at module level.

SSE (`stream.py`) is best covered by an integration test using `httpx.AsyncClient` against the running FastAPI app rather than unit-testing the generator in isolation, since its behavior (version-gated sends, disconnect handling) only makes sense against a real cache and a real request lifecycle.

## 12. Extension Points

- **New provider**: implement `MarketDataSource`, add a branch in `create_market_data_source`. No changes needed to `PriceCache`, `stream.py`, or any consumer.
- **Historical/detail-view data** (e.g., a candlestick chart for the selected ticker): the Massive client already exposes `list_aggs()` / `get_previous_close_agg()` (see `planning/archive/MASSIVE_API.md` §4) for this; the simulator would need an equivalent that replays or synthesizes bars — not required for the current SSE-driven sparkline/line-chart design, which accumulates history client-side from the stream.
- **Per-user watchlists**: `MarketDataSource` already tracks an arbitrary, mutable ticker set — multi-user support would mean the union of all users' watchlists is passed to a single shared source (matching the "shared price cache" note in `planning/PLAN.md` §6), not one source per user.
