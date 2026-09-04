# Market Data Backend — Detailed Design

**Component:** `backend/app/market/`
**Status:** Implemented and tested (73 tests passing, 91% statement coverage on the package).
**Audience:** Backend engineers extending the market data layer; downstream agents (portfolio, watchlist, chat) consuming it.

This document is the full design of the market data subsystem: the unified data-source interface, the built-in Geometric Brownian Motion (GBM) simulator, the Massive (formerly Polygon.io) REST client, the shared price cache, and the Server-Sent Events (SSE) endpoint that pushes prices to the browser. Every code block below is either verbatim from the implementation or a worked example that runs against it.

Numerical claims in §6 (volatility calibration, correlation targets, positive-definiteness of the correlation matrix) were verified by running the implementation; the verification script is reproduced in [Appendix A](#appendix-a--reproducing-the-calibration-checks) so it can be re-run in a Jupyter notebook.

---

## Table of Contents

1. [Design goals and constraints](#1-design-goals-and-constraints)
2. [Architecture](#2-architecture)
3. [File structure](#3-file-structure)
4. [The data model — `PriceUpdate`](#4-the-data-model--priceupdate)
5. [The shared price cache — `PriceCache`](#5-the-shared-price-cache--pricecache)
6. [The unified interface — `MarketDataSource`](#6-the-unified-interface--marketdatasource)
7. [The simulator](#7-the-simulator)
8. [The Massive API client](#8-the-massive-api-client)
9. [The factory](#9-the-factory)
10. [SSE streaming](#10-sse-streaming)
11. [Application integration](#11-application-integration)
12. [Watchlist coordination](#12-watchlist-coordination)
13. [Configuration reference](#13-configuration-reference)
14. [Testing strategy](#14-testing-strategy)
15. [Error handling and edge cases](#15-error-handling-and-edge-cases)
16. [Known gaps and recommended refinements](#16-known-gaps-and-recommended-refinements)
- [Appendix A — Reproducing the calibration checks](#appendix-a--reproducing-the-calibration-checks)
- [Appendix B — Consuming the stream from the frontend](#appendix-b--consuming-the-stream-from-the-frontend)
- [References](#references)

---

## 1. Design goals and constraints

| Goal | How it is met |
|---|---|
| The app must run with **no API key** | The simulator is the default; `MASSIVE_API_KEY` is optional |
| Downstream code must be **source-agnostic** | Both sources implement one abstract base class and write to one cache; nothing downstream imports either concrete class |
| Prices must feel **alive** (~500 ms updates) | Simulator ticks at 500 ms; SSE pushes on change at up to 2 Hz |
| Real data must respect **rate limits** | A single batched snapshot call per poll; 15 s default interval fits the 5 req/min free tier with margin |
| A blocking third-party SDK must not stall the server | The synchronous `massive` REST client runs via `asyncio.to_thread` |
| Prices must be readable from **request handlers and background tasks** | The cache is guarded by a `threading.Lock`, not an asyncio lock |

Two constraints shape almost every decision below:

1. **The `massive` SDK is synchronous.** It is a blocking `requests`-based client, so it can never be called directly on the event loop. Everything about the polling loop follows from that.
2. **There is exactly one producer and many consumers.** One background task writes prices; SSE connections, trade execution, and portfolio valuation all read. That asymmetry is why a plain mutex is sufficient and why the cache — not the data source — is the integration point.

---

## 2. Architecture

```
                     MASSIVE_API_KEY set?
                             │
              ┌──────────────┴──────────────┐
              │ no                          │ yes
              ▼                             ▼
    ┌───────────────────┐        ┌─────────────────────┐
    │ SimulatorDataSource│        │  MassiveDataSource  │
    │  GBM, 500 ms tick  │        │ REST poll, 15 s     │
    │  (in-process)      │        │ asyncio.to_thread   │
    └─────────┬─────────┘        └──────────┬──────────┘
              │      both implement MarketDataSource      │
              └──────────────┬───────────────────────────┘
                             │ .update(ticker, price, ts)
                             ▼
                  ┌──────────────────────┐
                  │      PriceCache      │   dict[str, PriceUpdate]
                  │  threading.Lock      │   + monotonic version counter
                  └──────────┬───────────┘
            ┌────────────────┼────────────────┐
            ▼                ▼                ▼
   GET /api/stream/prices   trade execution   portfolio valuation
   (SSE, 500 ms, on change) (fill price)      (mark-to-market)
            │
            ▼
   EventSource in the browser
```

**Control flow, one lifetime of the app:**

1. `lifespan` startup creates a `PriceCache`, calls `create_market_data_source(cache)`, reads the watchlist from SQLite, and `await source.start(tickers)`.
2. The source's background task writes into the cache forever, at its own cadence.
3. Each SSE client runs its own generator, polling the cache's `version` counter and emitting a frame only when it changed.
4. Watchlist mutations call `source.add_ticker()` / `source.remove_ticker()`.
5. `lifespan` shutdown calls `await source.stop()`, which cancels and awaits the task.

**The key invariant:** the data source never returns prices to a caller. It only pushes into the cache. Consumers only read the cache. This is what makes the simulator and the Massive client fully interchangeable despite having completely different cadences (2 Hz vs 0.067 Hz), timestamp semantics (local clock vs exchange trade time), and failure modes.

---

## 3. File structure

```
backend/
├── app/
│   └── market/
│       ├── __init__.py          # Public API re-exports
│       ├── models.py            # PriceUpdate
│       ├── cache.py             # PriceCache
│       ├── interface.py         # MarketDataSource (ABC)
│       ├── seed_prices.py       # Seed prices, GBM params, correlation groups
│       ├── simulator.py         # GBMSimulator + SimulatorDataSource
│       ├── massive_client.py    # MassiveDataSource
│       ├── factory.py           # create_market_data_source()
│       └── stream.py            # create_stream_router() — SSE endpoint
├── tests/market/                # 6 test modules, 73 tests
└── market_data_demo.py          # Rich terminal dashboard (manual smoke test)
```

The package exposes a deliberately small surface:

```python
# backend/app/market/__init__.py
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

`GBMSimulator`, `SimulatorDataSource`, and `MassiveDataSource` are intentionally **not** re-exported. Downstream code should never name a concrete source — it receives one from the factory and holds it as a `MarketDataSource`. (Tests import them directly, which is the one legitimate exception.)

---

## 4. The data model — `PriceUpdate`

One immutable record leaves the market data layer. Everything downstream — SSE payloads, trade fills, portfolio marks — works with it.

```python
# backend/app/market/models.py
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

**Design decisions:**

- **`frozen=True`** — an update handed to an SSE generator, a trade handler, and a valuation routine simultaneously cannot be mutated by any of them. This is what makes `get_all()`'s shallow copy safe.
- **`slots=True`** — no per-instance `__dict__`. At 2 updates/second × 10 tickers this allocates ~72,000 objects an hour; slots keeps that cheap and immediately collectable.
- **Derived fields are properties, not stored fields.** `change`, `change_percent`, and `direction` are pure functions of `price` and `previous_price`. Storing them would allow the record to become internally inconsistent; computing them costs one subtraction on access.
- **`previous_price`, not `previous_close`.** This is the *last tick's* price, which is what drives the frontend's green/red flash. Day-over-day change (which needs the previous session's close) is a separate concern and is not modelled here — see §16.
- **Unix seconds, float.** The simulator uses `time.time()`; the Massive client divides the API's millisecond timestamps by 1000. Both land in the same unit, so a consumer never has to ask which source produced a record.

**Zero-division guard.** `change_percent` returns `0.0` when `previous_price == 0`. That case only arises if a source ever writes a price of 0.0 (a malformed API response), but the guard means a bad upstream tick degrades a number rather than raising inside an SSE generator.

---

## 5. The shared price cache — `PriceCache`

The cache is the single point of truth and the only object shared between the producer task and every consumer.

```python
# backend/app/market/cache.py
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        Automatically computes direction and change from the previous price.
        If this is the first update for the ticker, previous_price == price (direction='flat').
        """
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
        """Get the latest price for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: get just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache (e.g., when removed from watchlist)."""
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Current version counter. Useful for SSE change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Why the cache computes `previous_price`

Neither source has to track prior state. The simulator hands over a price; the Massive client hands over a price; the cache does the differencing. This is what lets `PriceUpdate.direction` be meaningful regardless of source, and it is why a source can restart without producing a spurious direction — the first update for a ticker sets `previous_price == price`, giving `direction == "flat"` and `change == 0.0`.

### Why a version counter

The SSE generator needs to answer "has anything changed since my last frame?" without comparing dictionaries. A monotonically increasing integer answers it in one comparison:

```python
current = price_cache.version
if current != last_version:
    last_version = current
    ...emit frame...
```

With the simulator this is nearly always true (every tick writes 10 updates, so `version` jumps by 10 every 500 ms). With the **Massive** client it matters a great deal: prices update every 15 seconds, so 29 of every 30 SSE poll iterations skip serialization entirely. The counter is what makes one SSE implementation appropriate for both cadences.

The counter is never exposed to clients and never persisted — it is a change token, not a sequence number, and it resets to 0 on restart. Consumers must only compare it for equality, never interpret its magnitude.

### Why `threading.Lock` and not `asyncio.Lock`

The Massive client executes its HTTP call in a worker thread via `asyncio.to_thread`. Writes therefore originate off the event loop. An `asyncio.Lock` provides no protection against a genuine OS thread; a `threading.Lock` does. The critical sections are a dict lookup plus an assignment, so contention at 10–50 tickers is immaterial.

### Rounding

Prices are rounded to 2 decimals **as they enter the cache**, not in the simulator's internal state (see §7.4). The cache is the display and settlement boundary: a trade fills at a cent-precision price, and the frontend renders a cent-precision price. Keeping full precision inside the simulator and rounding only at the boundary prevents rounding error accumulating in the price path.

---

## 6. The unified interface — `MarketDataSource`

```python
# backend/app/market/interface.py
from __future__ import annotations

from abc import ABC, abstractmethod


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
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task that periodically writes to the PriceCache.
        Must be called exactly once. Calling start() twice is undefined behavior.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times. After stop(), the source will not write
        to the cache again.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present.

        The next update cycle will include this ticker.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. No-op if not present.

        Also removes the ticker from the PriceCache.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

### Contract details that both implementations honour

| Rule | Simulator | Massive |
|---|---|---|
| `start()` seeds the cache before returning | Writes seed prices synchronously | Performs one blocking poll before spawning the loop |
| `stop()` is idempotent | Guards on `self._task and not self._task.done()` | Same guard, plus drops the client |
| `stop()` awaits cancellation | `await self._task` inside `except CancelledError` | Same |
| `add_ticker()` is a no-op if present | `GBMSimulator.add_ticker` early-returns | `if ticker not in self._tickers` |
| `remove_ticker()` also clears the cache | Yes | Yes |
| The loop never dies on error | `try/except` inside the loop body | `try/except` inside `_poll_once` |

That last row is the most important. A background task that raises escapes to the event loop's exception handler and simply stops — the app keeps serving requests but prices freeze silently. Both loops therefore catch broadly, log, and continue to the next iteration.

`add_ticker` and `remove_ticker` are `async` even though the simulator's implementations do no awaiting. This is deliberate: the interface must accommodate an implementation that needs I/O (for example, a future WebSocket source sending a subscribe frame), and callers should not have to change when the source changes.

**Why `start()` seeds the cache.** Without an initial write, the first SSE frame after startup would be empty and the watchlist would render blank until the first tick. Both sources write at least one price per ticker before `start()` returns, so the frontend has data on its first frame.

---

## 7. The simulator

The default source. No network, no key, deterministic under a seeded RNG, and calibrated so that a simulated trading day has a realistic range.

### 7.1 The model

Prices follow Geometric Brownian Motion, the model underlying Black–Scholes–Merton option pricing [1, 2]. Under GBM the price $S_t$ solves the stochastic differential equation

```
dS = mu * S dt + sigma * S dW
```

whose exact solution — obtained by applying Itô's lemma to `log S` [3] — is the discretisation used here:

```
S(t + dt) = S(t) * exp( (mu - sigma^2 / 2) * dt  +  sigma * sqrt(dt) * Z ),    Z ~ N(0, 1)
```

Three properties make it the right choice for this application:

1. **Prices cannot go negative.** The multiplicative `exp(...)` factor is strictly positive, so no guard clause is needed anywhere in the codebase.
2. **Log-returns are normally distributed**, which is the standard first-order description of equity returns and produces price paths that look like real ones at a glance.
3. **The discretisation is exact, not an approximation.** Because the solution of the SDE is known in closed form, sampling it at any `dt` introduces no discretisation bias — unlike an Euler–Maruyama step on the SDE directly. This matters because the loop runs millions of steps over a long session.

The `- sigma^2 / 2` term is the Itô correction. Without it the *median* path would grow at `mu` but the *mean* would grow at `mu + sigma^2/2`; with it, `E[S_t] = S_0 * exp(mu * t)`, so `mu` means what it says.

### 7.2 The time step

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.479e-8
```

252 trading days × 6.5 hours × 3600 seconds is the standard trading-year convention. Wall-clock time therefore maps 1:1 to simulated market time: one real second of watching the dashboard is one second of simulated trading. Verified consequences (numbers from Appendix A):

| Ticker | `sigma` | Per-tick log-return SD | Per-tick move on seed price | Daily SD (`sigma`/√252) |
|---|---|---|---|---|
| AAPL | 0.22 | 6.41e-05 | ~$0.012 on $190 | 1.39% |
| TSLA | 0.50 | 1.46e-04 | ~$0.036 on $250 | 3.15% |
| V | 0.17 | 4.95e-05 | ~$0.014 on $280 | 1.07% |

A single 500 ms tick moves a stock by roughly one to four cents — small enough that the display looks like a real tape, large enough that the flash animation fires. Over a full simulated session (46,800 ticks) the paths drift by a percent or a few, which is exactly the intended intraday range.

**A consequence worth knowing:** because per-tick moves are close to the one-cent rounding granularity, a meaningful fraction of ticks round to no visible change and render as `direction == "flat"`. This is realistic behaviour, not a bug — real tape for a $190 stock does not tick on every print either.

### 7.3 Correlated moves

Independent random draws per ticker would produce a watchlist where AAPL rises while MSFT falls, every tick, forever — visibly wrong. Real equities share systematic risk factors, so the simulator draws **correlated** normals.

Given a target correlation matrix `C`, take its Cholesky factor `L` such that `L @ L.T == C`. If `Z` is a vector of independent standard normals, then `L @ Z` is a vector of standard normals with correlation exactly `C` [4]:

```
Cov(L Z) = L Cov(Z) L^T = L I L^T = L L^T = C
```

The correlation structure is sector-based:

```python
# backend/app/market/seed_prices.py
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6     # Tech stocks move together
INTRA_FINANCE_CORR = 0.5  # Finance stocks move together
CROSS_GROUP_CORR = 0.3    # Between sectors / unknown tickers
TSLA_CORR = 0.3           # TSLA does its own thing
```

```python
@staticmethod
def _pairwise_correlation(t1: str, t2: str) -> float:
    tech = CORRELATION_GROUPS["tech"]
    finance = CORRELATION_GROUPS["finance"]

    # TSLA is in tech set but behaves independently
    if t1 == "TSLA" or t2 == "TSLA":
        return TSLA_CORR

    if t1 in tech and t2 in tech:
        return INTRA_TECH_CORR
    if t1 in finance and t2 in finance:
        return INTRA_FINANCE_CORR

    return CROSS_GROUP_CORR
```

The TSLA branch is checked **first**, deliberately: TSLA is a member of the `tech` set (so it is grouped correctly for any future factor logic) but is pinned to the baseline 0.3 against everything, giving it visibly independent price action.

**Positive-definiteness.** `numpy.linalg.cholesky` raises `LinAlgError` on a matrix that is not positive definite [5], which would kill `_rebuild_cholesky()` and, with it, `add_ticker()`. The block structure here is safe: it is a block-equicorrelation matrix with all off-diagonals in [0.3, 0.6]. Verified minimum eigenvalues (Appendix A):

| Ticker set | n | Min eigenvalue | Cholesky |
|---|---|---|---|
| Default watchlist | 10 | +0.400 | OK |
| Tech only | 7 | +0.400 | OK |
| Default + 20 unknown tickers | 30 | +0.400 | OK |
| Finance + 10 unknown tickers | 12 | +0.500 | OK |

The margin is comfortable and does not degrade with `n`, because adding unknown tickers only adds rows at the baseline 0.3 correlation. A general equicorrelation matrix is positive definite whenever `rho > -1/(n-1)`, which holds for any positive `rho`. **Any future change to these constants must keep every value strictly below 1.0 and re-run the eigenvalue check** — a matrix with an off-diagonal of 1.0 is singular and will raise.

### 7.4 `GBMSimulator`

```python
# backend/app/market/simulator.py
class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices."""

    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8

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

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Returns {ticker: new_price}.

        This is the hot path — called every 500ms. Keep it fast.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        # Generate n independent standard normal draws
        z_independent = np.random.standard_normal(n)

        # Apply Cholesky to get correlated draws
        if self._cholesky is not None:
            z_correlated = self._cholesky @ z_independent
        else:
            z_correlated = z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            # GBM: S(t+dt) = S(t) * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random event: ~0.1% chance per tick per ticker
            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign
                logger.debug(...)

            result[ticker] = round(self._prices[ticker], 2)

        return result
```

Three details in `step()` carry real weight:

- **`self._prices` holds unrounded state; only the returned dict is rounded.** Rounding the internal price would inject a bias of up to half a cent into every one of the ~46,800 steps in a simulated day, and that error compounds multiplicatively. Rounding only the output keeps the path exact.
- **One vectorised `standard_normal(n)` draw plus one matrix–vector product per tick**, then a Python loop for the per-ticker exponential. At n ≤ 50 and 2 Hz the loop is free; vectorising the exponential too would save microseconds and cost readability.
- **`self._cholesky is None` when n ≤ 1.** A 1×1 correlation matrix is `[[1.0]]` and its factor is the identity, so the branch skips a pointless multiplication and lets the single-ticker case work before any matrix is built.

The shock event fires with probability 0.001 per ticker per tick. With 10 tickers at 2 ticks/second, the expected rate is `10 × 2 × 0.001 = 0.02` events/second — one visible 2–5% jump somewhere on the board roughly every 50 seconds. It is applied *after* the GBM step, so it is a genuine jump superimposed on the diffusion rather than a modification of the diffusion (a crude jump-diffusion, in the spirit of Merton's model [6], without the compensator term — this is for visual drama, not for pricing anything).

Ticker management rebuilds the factorisation:

```python
    def add_ticker(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add a ticker without rebuilding Cholesky (for batch initialization)."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)
```

`_add_ticker_internal` exists so that constructing the simulator with 10 tickers performs **one** O(n³) factorisation rather than ten. `dict(DEFAULT_PARAMS)` copies the defaults so that a later per-ticker tweak cannot mutate the module-level constant shared by every unknown ticker.

`get_tickers()` is public specifically so `SimulatorDataSource` does not have to reach into `_tickers`.

### 7.5 Seed data

```python
# backend/app/market/seed_prices.py
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00, "TSLA": 250.00,
    "NVDA": 800.00, "META": 500.00, "JPM": 195.00, "V": 280.00, "NFLX": 600.00,
}

TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},  # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},  # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},  # Low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},  # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}
```

Seed prices are round-number approximations of the tickers' levels at project creation; they are illustrative starting points for a simulation, not quoted market data. The `sigma` values encode the intended qualitative ordering — a payments network is less volatile than a bank, which is less volatile than a mega-cap tech name, which is less volatile than TSLA — at magnitudes in the range typically observed for large-cap US equities. Unknown tickers start at a uniform random price in [50, 300] with the default parameters, so an obscure symbol added through the chat assistant still behaves plausibly.

These are **simulation inputs, not estimates**. If the simulator were ever to be used for anything quantitative, `sigma` should be estimated from historical returns (for example as the annualised standard deviation of daily log returns) rather than hand-set.

### 7.6 `SimulatorDataSource`

The async wrapper that makes `GBMSimulator` a `MarketDataSource`.

```python
class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator."""

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
        # Seed the cache with initial prices so SSE has data immediately
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(tickers))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Simulator stopped")

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            # Seed cache immediately so the ticker has a price right away
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
            logger.info("Simulator: added ticker %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        """Core loop: step the simulation, write to cache, sleep."""
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

Points to note:

- **`asyncio.sleep` is outside the `try`.** If a step raises, the loop still sleeps before retrying rather than spinning at 100% CPU logging the same traceback thousands of times a second.
- **`stop()` awaits the cancelled task.** `task.cancel()` only *requests* cancellation; without the await, `stop()` can return while the loop is still mid-tick, and shutdown races with the interpreter tearing down.
- **`add_ticker` writes to the cache immediately.** A ticker added through `POST /api/watchlist` has a price before the response is even serialized, so the UI can render it and a trade against it can fill straight away.
- **`remove_ticker` clears the cache outside the `if self._sim` guard.** Even if the source was never started, removal still purges any stale entry.

---

## 8. The Massive API client

Used when `MASSIVE_API_KEY` is set. Massive is the current name of Polygon.io, which rebranded in October 2025; the Python SDK is `massive` and defaults to `https://api.massive.com`, with `api.polygon.io` still supported [7, 8].

### 8.1 The polling budget

The design is shaped by one number: the free tier allows **5 REST requests per minute** [9].

| Approach | Requests/min at 10 tickers | Verdict |
|---|---|---|
| One `get_last_trade` per ticker per poll | 10 per poll — over budget in a single cycle | Unusable |
| Batched snapshot every 15 s | 4 | **Chosen** — 20% headroom under the free-tier cap |
| Batched snapshot every 2 s | 30 | Paid tiers only |

The full-market snapshot endpoint returns every requested ticker in **one** call [10], which is what makes a 10-ticker (or 50-ticker) watchlist affordable. The endpoint is:

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT
```

Massive's unified snapshot caps a single request at 250 symbols [11] — far above any plausible FinAlly watchlist, so no request-splitting logic is needed. If the watchlist ever exceeds that, `_fetch_snapshots` would need to chunk the ticker list, which would multiply the request count and require a longer poll interval.

### 8.2 Implementation

```python
# backend/app/market/massive_client.py
from __future__ import annotations

import asyncio
import logging

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Rate limits:
      - Free tier: 5 req/min → poll every 15s (default)
      - Paid tiers: higher limits → poll every 2-5s
    """

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

        # Do an immediate first poll so the cache has data right away
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval",
            len(tickers), self._interval,
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Execute one poll cycle: fetch snapshots, update cache."""
        if not self._tickers or not self._client:
            return

        try:
            # The Massive RESTClient is synchronous — run in a thread to
            # avoid blocking the event loop.
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    # Massive timestamps are Unix milliseconds → convert to seconds
                    timestamp = snap.last_trade.timestamp / 1000.0
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e,
                    )
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — the loop will retry on the next interval.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### 8.3 Why `asyncio.to_thread`

`RESTClient` is a blocking HTTP client. Calling it directly from a coroutine would freeze the entire event loop for the duration of the request — every SSE stream, every API request, every other background task — for potentially hundreds of milliseconds or, on a hung connection, seconds. `asyncio.to_thread` runs the callable in the default executor and awaits the result without blocking the loop [12].

This is precisely why `PriceCache` is guarded by a `threading.Lock`: `_fetch_snapshots` returns to the event loop thread, but the pattern means market data genuinely crosses a thread boundary and the cache must be safe for it.

### 8.4 Two levels of error handling

```
_poll_once
├── outer try  → whole-poll failures: 401 bad key, 429 rate limit, timeouts, DNS
│                 log at ERROR, keep the loop alive, retry next interval
└── inner try  → per-snapshot failures: missing last_trade, null price, bad shape
                  log at WARNING, skip that ticker, keep processing the rest
```

The split matters. A single malformed snapshot — a thinly traded symbol with no trade yet today, so `last_trade` is `None` — must not discard the nine good prices in the same response. Conversely, an expired API key must not crash the poller; it logs and retries, so correcting the key and restarting is the whole fix.

The client is **not** re-raised or surfaced to the user through an error channel. The observable symptom of a bad key is that prices never appear while the SSE connection stays healthy. §16 notes the improvement.

### 8.5 Timestamp semantics

Massive returns Unix **milliseconds**; `PriceUpdate.timestamp` is Unix **seconds**, so `_poll_once` divides by 1000.0. Note the semantic difference from the simulator: the Massive timestamp is the **exchange trade time**, so outside market hours it is the time of the last trade — possibly many hours old — not the poll time. Consumers that display a timestamp should treat it as "as of", not "fetched at".

### 8.6 Ticker normalisation

`add_ticker`/`remove_ticker` apply `.upper().strip()` because the API is case-sensitive and user input arrives from a text box and from the LLM. The simulator does not normalise, since its keys are internal. **Normalisation belongs in the watchlist route** so that both sources and the database agree on the canonical form — see §16.

---

## 9. The factory

```python
# backend/app/market/factory.py
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment variables.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
    - Otherwise → SimulatorDataSource (GBM simulation)

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

`.strip()` on the environment variable is what makes `MASSIVE_API_KEY=` in `.env` — the committed default — select the simulator rather than constructing a client with an empty key that 401s forever. A whitespace-only value is treated the same way.

The factory returns an **unstarted** source. Construction is synchronous and cheap; `start()` is async and does I/O (the Massive path performs a blocking poll). Keeping them separate means the factory can be called anywhere, including in a synchronous test, and the caller controls when the network is touched.

Both concrete classes are imported at module level, not lazily. `massive` is a declared core dependency in `pyproject.toml`, so it is always installed; a lazy import would only serve to make `unittest.mock.patch` targets not exist at module scope, which is exactly the test fragility that an earlier revision of this code had.

The log line at startup is the single most useful diagnostic in the subsystem — it is how anyone determines, in one glance at the container logs, whether they are looking at real or simulated prices.

---

## 10. SSE streaming

### 10.1 Why SSE

Server-Sent Events is a browser-native, one-way server→client push protocol over plain HTTP [13]. Prices flow only from server to client, so the bidirectional machinery of WebSockets buys nothing here, while SSE brings two things for free: automatic reconnection with a server-controlled backoff, and ordinary HTTP semantics (same origin, same port, no upgrade handshake, no proxy configuration).

### 10.2 Implementation

```python
# backend/app/market/stream.py
router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create the SSE streaming router with a reference to the price cache."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering if proxied
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE-formatted price events."""
    # Tell the client to retry after 1 second if the connection drops
    yield "retry: 1000\n\n"

    last_version = -1
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
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
        logger.info("SSE stream cancelled for: %s", client_ip)
```

The router is created by a **factory function** so the `PriceCache` is captured in a closure. There is no module-level global holding application state, and tests can build a router over a purpose-built cache.

### 10.3 The three headers

| Header | Why |
|---|---|
| `Cache-Control: no-cache` | Prevents any intermediary from caching a stream that by definition never repeats |
| `Connection: keep-alive` | Explicit for HTTP/1.1 intermediaries |
| `X-Accel-Buffering: no` | Disables nginx response buffering. **Without it, a reverse proxy buffers the stream and the client sees nothing until the buffer fills** — the single most common way an SSE deployment silently fails |

### 10.4 Wire format

The first thing written on every connection is the retry directive:

```
retry: 1000

```

This sets the browser's `EventSource` reconnection delay to 1000 ms [13, 14]. It is sent first so it applies even if the connection drops before the first data frame.

Each subsequent frame is one `data:` line terminated by a blank line — the field/blank-line framing the SSE specification defines [13]:

```
data: {"AAPL":{"ticker":"AAPL","price":190.42,"previous_price":190.38,"timestamp":1770000000.12,"change":0.04,"change_percent":0.021,"direction":"up"},"GOOGL":{...}}

```

**The payload is the complete price map, not a delta.** This is deliberate:

- A client that connects mid-session is immediately whole — no snapshot-then-delta protocol, no resync logic after a reconnect.
- A dropped frame is self-healing; the next frame carries the full state.
- The cost is bandwidth: ~180 bytes per ticker, so ~1.8 KB per frame for 10 tickers, at up to 2 frames/second — under 4 KB/s per client. For a single-user desktop app this is irrelevant, and it removes an entire category of state-synchronisation bugs.

Untyped `data:` frames dispatch to `EventSource.onmessage`, so no `addEventListener` for named event types is needed on the client.

### 10.5 Poll-and-push, not event-driven

The generator polls the cache on a timer rather than being woken by the producer. This keeps the wire cadence **regular and decoupled from the source's cadence** — which is what the frontend's sparklines need, since they accumulate SSE frames into evenly spaced series. It also means the simulator (2 Hz) and Massive (0.067 Hz) need no different handling: the version check suppresses redundant frames, so a Massive-backed stream emits roughly one frame per 15 seconds while the same code emits ~2/second under the simulator.

The trade-off is up to `interval` (500 ms) of added latency between a cache write and the frame that carries it. For a price display this is imperceptible; it would not be acceptable for order acknowledgements, which is why trade responses return the fill directly rather than waiting for the stream.

### 10.6 Disconnect handling

`await request.is_disconnected()` is checked every iteration [15]. Without it, a generator whose client closed the tab keeps running — polling, serializing, and yielding into a dead socket — forever, and each abandoned tab leaks another one. Checking on every iteration bounds the leak to at most one poll interval.

`asyncio.CancelledError` is caught separately for the server-initiated case (shutdown, or the ASGI server tearing the connection down), so shutdown logs cleanly instead of emitting a traceback per connected client.

---

## 11. Application integration

The subsystem is wired into FastAPI through the `lifespan` context manager [16], which owns startup and shutdown of the background task.

```python
# backend/app/main.py
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from app.market import (
    MarketDataSource,
    PriceCache,
    create_market_data_source,
    create_stream_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    source = create_market_data_source(price_cache)   # reads MASSIVE_API_KEY
    app.state.market_source = source

    initial_tickers = await load_watchlist_tickers()  # SELECT ticker FROM watchlist
    await source.start(initial_tickers)

    yield

    # --- shutdown ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)
```

**Ordering constraint.** `create_stream_router(price_cache)` needs the cache instance, and the cache is created inside `lifespan`. So the router is registered inside `lifespan`, before the `yield` — routes added before the first request are served normally:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    source = create_market_data_source(price_cache)
    app.state.market_source = source
    await source.start(await load_watchlist_tickers())

    app.include_router(create_stream_router(price_cache))   # <- here

    yield

    await source.stop()
```

The static-file mount for the Next.js export must be added **after** the API routers, because a catch-all mount at `/` would otherwise shadow `/api/*`:

```python
app.mount("/", StaticFiles(directory="static", html=True), name="static")
```

### Dependency injection

State lives on `app.state`; handlers receive it through small dependency functions that read from the request, which keeps them testable without a module-level global.

```python
def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_market_source(request: Request) -> MarketDataSource:
    return request.app.state.market_source
```

**Trade execution** reads the fill price from the cache:

```python
@router.post("/api/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    price = price_cache.get_price(trade.ticker)
    if price is None:
        raise HTTPException(
            status_code=400,
            detail=f"No price available for {trade.ticker} yet — try again in a moment.",
        )
    # ... validate cash / shares, insert into trades, upsert positions, snapshot ...
```

**Portfolio valuation** marks positions to the cache in one pass:

```python
def value_portfolio(positions: list[Position], cash: float, cache: PriceCache) -> dict:
    prices = cache.get_all()          # one lock acquisition, immutable records
    market_value = 0.0
    for p in positions:
        update = prices.get(p.ticker)
        price = update.price if update else p.avg_cost   # fall back to cost basis
        market_value += p.quantity * price
    return {"cash": cash, "market_value": market_value, "total_value": cash + market_value}
```

Calling `get_all()` once — rather than `get_price()` per position — takes the lock a single time and guarantees every position is valued against the **same** snapshot. Valuing positions one at a time can straddle a cache write and produce a total that never actually existed.

---

## 12. Watchlist coordination

The watchlist is stored in SQLite; the tracked ticker set lives in the data source. Every mutation must touch both, in this order.

**Adding:**

```
POST /api/watchlist {"ticker": "PYPL"}
  1. normalise      → "PYPL"
  2. INSERT INTO watchlist (idempotent via UNIQUE(user_id, ticker))
  3. await source.add_ticker("PYPL")
       simulator: seeds a price, rebuilds Cholesky, writes the cache → price available now
       massive:   appends to the ticker list → price available after the next poll (≤15 s)
  4. 200 with the ticker and its current price, if any
```

```python
@router.post("/api/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
    price_cache: PriceCache = Depends(get_price_cache),
):
    ticker = payload.ticker.upper().strip()
    await db.insert_watchlist_entry(ticker)   # ON CONFLICT DO NOTHING
    await source.add_ticker(ticker)
    return {"ticker": ticker, "price": price_cache.get_price(ticker)}
```

The database write comes first so that a crash between the two steps leaves a ticker that will be picked up on the next startup, rather than a tracked ticker that vanishes on restart. Both operations are idempotent, so a retry is harmless.

**Removing — and the position edge case:**

A ticker removed from the watchlist may still be held. If tracking stops, the cache entry is dropped and the position can no longer be marked to market — the portfolio total silently becomes wrong. The route must check:

```python
@router.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    ticker = ticker.upper().strip()
    await db.delete_watchlist_entry(ticker)

    # Keep streaming prices for anything we still hold, or valuation breaks.
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)

    return {"status": "ok", "ticker": ticker}
```

The mirror of this rule belongs in the sell path: **a position closed to zero, for a ticker not on the watchlist, should stop being tracked.** Without it, tickers accumulate in the source across a long session. This is a small cleanup in the trade handler, not a change to the market layer.

**LLM-driven changes take the same path.** Watchlist changes returned in the chat structured output must call these same route handlers (or the same service functions), never `source.add_ticker()` directly — otherwise the database and the tracked set diverge.

---

## 13. Configuration reference

| Parameter | Where | Default | Effect |
|---|---|---|---|
| `MASSIVE_API_KEY` | env var | `""` | Non-empty (after strip) selects `MassiveDataSource`; otherwise the simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | Simulator tick period |
| `event_probability` | `SimulatorDataSource` / `GBMSimulator` | `0.001` | Shock chance per ticker per tick (~1 event per 50 s across 10 tickers) |
| `dt` | `GBMSimulator.__init__` | `8.479e-8` | GBM step as a fraction of a trading year; ties simulated time to wall-clock |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Massive poll period — 4 req/min, inside the 5 req/min free tier |
| `interval` | `_generate_events` | `0.5` s | SSE poll/emit period |
| retry directive | `_generate_events` | `1000` ms | Browser `EventSource` reconnect delay |
| `SEED_PRICES`, `TICKER_PARAMS` | `seed_prices.py` | see §7.5 | Simulator starting prices and per-ticker `mu`/`sigma` |
| correlation constants | `seed_prices.py` | 0.6 / 0.5 / 0.3 | Sector correlation structure |

**Tuning for a paid Massive tier:** the only change needed is `poll_interval`. Paid tiers lift the request cap [9], so 2–5 s is reasonable. The SSE side needs no change — the version counter already suppresses frames between polls.

---

## 14. Testing strategy

Current state: **73 tests, all passing; 91% statement coverage** across `app/market/`.

| Module | Coverage | Notes |
|---|---|---|
| `models.py`, `cache.py`, `interface.py`, `seed_prices.py`, `factory.py`, `__init__.py` | 100% | |
| `simulator.py` | 98% | Uncovered: the duplicate-ticker guard in `_add_ticker_internal`, the exception branch in `_run_loop` |
| `massive_client.py` | 94% | Uncovered: warning branch for malformed snapshots, one client call path |
| `stream.py` | 33% | **Untested** — see §16 |

```bash
cd backend
uv sync --extra dev
uv run --extra dev pytest -q --cov=app --cov-report=term-missing
uv run --extra dev ruff check app/ tests/
```

Tests use `asyncio_mode = "auto"` (`pyproject.toml`), so `async def test_*` functions need no decorator.

### 14.1 Testing stochastic code

The simulator is random, so assertions must be **structural or statistical**, never on specific values.

```python
def test_step_returns_positive_prices():
    sim = GBMSimulator(["AAPL", "GOOGL"])
    for _ in range(100):
        for price in sim.step().values():
            assert price > 0          # guaranteed by exp(); a real invariant


def test_prices_move_but_stay_plausible():
    sim = GBMSimulator(["AAPL"], event_probability=0.0)   # shocks off
    start = sim.get_price("AAPL")
    for _ in range(1000):
        sim.step()
    end = sim.get_price("AAPL")
    assert end != start                       # it moved
    assert 0.5 * start < end < 2.0 * start    # within a sane band


def test_cholesky_built_for_full_default_watchlist():
    sim = GBMSimulator(list(SEED_PRICES))     # all 10 — must not raise LinAlgError
    assert len(sim.step()) == 10
```

Statistical properties are asserted with generous tolerances over many samples — the correlation and volatility checks in Appendix A are the template. Seed the RNG (`np.random.seed`, `random.seed`) in any test whose assertion is tight enough to flake.

### 14.2 Testing the cache

```python
def test_first_update_is_flat():
    cache = PriceCache()
    u = cache.update("AAPL", 190.0)
    assert u.previous_price == 190.0
    assert u.direction == "flat"
    assert u.change == 0.0


def test_direction_and_version_track_updates():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    v1 = cache.version
    u = cache.update("AAPL", 191.0)
    assert u.direction == "up"
    assert u.change == 1.0
    assert cache.version > v1


def test_concurrent_writers_do_not_lose_updates():
    """The lock's reason for existing: writes arrive from a worker thread."""
    import threading
    cache = PriceCache()

    def writer(tag: str):
        for i in range(500):
            cache.update(f"T{tag}", 100.0 + i * 0.01)

    threads = [threading.Thread(target=writer, args=(str(i),)) for i in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(cache) == 4
    assert cache.version == 2000
```

### 14.3 Testing the async sources

```python
async def test_simulator_source_lifecycle():
    cache = PriceCache()
    source = SimulatorDataSource(cache, update_interval=0.01)

    await source.start(["AAPL", "GOOGL"])
    assert cache.get_price("AAPL") is not None      # seeded synchronously by start()

    v = cache.version
    await asyncio.sleep(0.05)
    assert cache.version > v                        # the loop is running

    await source.add_ticker("TSLA")
    assert "TSLA" in source.get_tickers()
    assert cache.get_price("TSLA") is not None      # seeded immediately

    await source.remove_ticker("TSLA")
    assert cache.get_price("TSLA") is None          # purged from the cache

    await source.stop()
    await source.stop()                             # idempotent
```

Use a short `update_interval` (10 ms) so timing assertions complete quickly.

### 14.4 Testing the Massive client

Never call the real API in tests. Mock at the `_fetch_snapshots` boundary — one seam, and the code under test still exercises `to_thread`, the timestamp conversion, and both error paths.

```python
from unittest.mock import MagicMock, patch


def _snapshot(ticker: str, price: float, ts_ms: int) -> MagicMock:
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = ts_ms
    return snap


async def test_poll_converts_ms_timestamps_to_seconds():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache)
    source._client = MagicMock()
    source._tickers = ["AAPL"]
    source._fetch_snapshots = MagicMock(return_value=[_snapshot("AAPL", 190.5, 1675190399000)])

    await source._poll_once()

    update = cache.get("AAPL")
    assert update.price == 190.5
    assert update.timestamp == 1675190399.0          # ms → s


async def test_malformed_snapshot_does_not_lose_good_ones():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache)
    source._client = MagicMock()
    source._tickers = ["AAPL", "BAD"]

    bad = MagicMock(); bad.ticker = "BAD"; bad.last_trade = None
    source._fetch_snapshots = MagicMock(return_value=[_snapshot("AAPL", 190.5, 1), bad])

    await source._poll_once()                        # must not raise

    assert cache.get_price("AAPL") == 190.5
    assert cache.get_price("BAD") is None


async def test_api_failure_keeps_the_poller_alive():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache)
    source._client = MagicMock()
    source._tickers = ["AAPL"]
    source._fetch_snapshots = MagicMock(side_effect=RuntimeError("401 Unauthorized"))

    await source._poll_once()                        # swallowed, logged
    assert len(cache) == 0
```

Because `massive` is a core dependency, `patch("app.market.massive_client.RESTClient")` resolves at module scope and needs no `create=True`.

### 14.5 Testing the factory

```python
def test_factory_selects_simulator_when_key_absent(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    assert isinstance(create_market_data_source(PriceCache()), SimulatorDataSource)


def test_whitespace_key_selects_simulator(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "   ")
    assert isinstance(create_market_data_source(PriceCache()), SimulatorDataSource)


def test_factory_selects_massive_when_key_present(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "abc123")
    assert isinstance(create_market_data_source(PriceCache()), MassiveDataSource)
```

Always use `monkeypatch.setenv`/`delenv` so the environment is restored after each test.

### 14.6 The missing SSE test

`stream.py` is the least-covered module and the one the frontend depends on most. A single integration test would close the gap:

```python
import httpx
from fastapi import FastAPI


async def test_sse_emits_retry_then_price_frames():
    cache = PriceCache()
    cache.update("AAPL", 190.0)

    app = FastAPI()
    app.include_router(create_stream_router(cache))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream("GET", "/api/stream/prices") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")

            chunks = []
            async for chunk in response.aiter_text():
                chunks.append(chunk)
                if len(chunks) >= 2:
                    break

    assert chunks[0].startswith("retry: 1000")
    assert '"AAPL"' in chunks[1]
    assert chunks[1].startswith("data: ")
    assert chunks[1].endswith("\n\n")
```

This requires adding `httpx` to the dev extras.

---

## 15. Error handling and edge cases

| Case | Behaviour | Rationale |
|---|---|---|
| **Empty watchlist at startup** | Both sources start cleanly; `step()` returns `{}`, `_poll_once()` early-returns; SSE sends the retry directive and then nothing (the `if prices:` guard suppresses empty frames) | Adding the first ticker starts the flow immediately |
| **Trade against an unpriced ticker** | `get_price()` returns `None`; the route returns HTTP 400 with a clear message | Only realistically reachable on the Massive path within the first poll window; the simulator seeds on add |
| **Invalid `MASSIVE_API_KEY`** | Every poll 401s, is logged at ERROR, and the loop continues; SSE stays connected with no data | Restarting with a corrected key is the whole fix; the ERROR log names the cause |
| **Rate limit exceeded (429)** | Same path as any poll failure — logged, retried next interval | The 15 s default keeps usage at 4 req/min, so this should only occur if the interval is lowered on a free key |
| **Malformed snapshot** | That ticker is skipped with a WARNING; all others in the response are processed | One bad symbol must not blank the board |
| **Client closes the tab** | `is_disconnected()` is true within one poll interval; the generator breaks and logs | Bounds abandoned generators to ≤500 ms of extra work |
| **Server shutdown with clients connected** | `CancelledError` is caught per generator; `source.stop()` cancels and awaits the producer | Clean logs, no orphaned tasks |
| **Non-positive-definite correlation matrix** | Would raise `LinAlgError` from `_rebuild_cholesky` and propagate out of `add_ticker` | Not reachable with the current constants (min eigenvalue +0.4); see the warning in §7.3 |
| **Floating-point drift in the price path** | Not a concern: `exp()` is numerically stable, prices stay positive by construction, and rounding happens only at the cache boundary | See §7.4 |
| **Unknown ticker added** | Starts at a random price in [50, 300] with default `mu`/`sigma` on the simulator; on Massive, a symbol that does not exist simply never appears in snapshot responses | No validation of ticker existence today — see §16 |

---

## 16. Known gaps and recommended refinements

These are open items, ordered by value. None block the current integration.

**1. `stream.py` has no tests (33% coverage).** The generator is the frontend's whole contract. Add the integration test from §14.6 and `httpx` to the dev extras.

**2. The module-level `router` in `stream.py` is a latent bug.** `router = APIRouter(...)` is created at import time and `create_stream_router()` registers `/prices` on it via closure. Calling the factory twice — which any test suite that builds two apps will do — registers the route twice on the same shared router. Move the construction inside the factory:

```python
def create_stream_router(price_cache: PriceCache) -> APIRouter:
    router = APIRouter(prefix="/api/stream", tags=["streaming"])   # per-call, not module-level

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        ...

    return router
```

**3. `PriceCache.version` reads without the lock.** Safe today — a CPython `int` read is atomic under the GIL — but inconsistent with every other accessor, and not guaranteed under a free-threaded build (PEP 703 [17]). One-line fix:

```python
    @property
    def version(self) -> int:
        with self._lock:
            return self._version
```

**4. Ticker normalisation is inconsistent.** `MassiveDataSource` upper-cases and strips; the simulator does not. Normalise once, in the watchlist route, before the database write — then both sources and SQLite agree on the canonical form and the two implementations behave identically. (Doing it in the route rather than in each source also stops `"aapl"` and `"AAPL"` becoming two rows.)

**5. No health signal for a failing data source.** A wrong API key looks exactly like a quiet market from the browser: SSE connected, no prices. Track `last_successful_poll` on `MassiveDataSource` and expose it via `/api/health`, so the header's connection indicator can distinguish "streaming" from "connected but the upstream is broken".

**6. No day-over-day change.** `PriceUpdate.previous_price` is the previous *tick*, which is what the flash animation needs, but the watchlist's "daily change %" needs the previous session's close. Massive returns `day.previous_close` in the same snapshot already being fetched; the simulator would record its session-start price. This means adding a `previous_close` field to `PriceUpdate` and a `session_open` map to the simulator — a small change, but it touches the SSE payload contract, so it should be agreed with the frontend before implementation.

**7. No ticker validation on add.** An invalid symbol is happily simulated at a random price, or silently never appears from Massive. A cheap improvement on the Massive path is a single-ticker snapshot call at add time, returning 404 if the symbol is unknown.

**8. Tracked tickers are never garbage-collected.** §12 keeps a ticker tracked while a position is open, but nothing untracks it when the position closes and it is off the watchlist. Add that cleanup to the sell path.

---

## Appendix A — Reproducing the calibration checks

This script verifies every numerical claim in §7. Run it from `backend/` (`uv run --extra dev python check.py`), or paste it into a Jupyter notebook cell with `backend/` on `sys.path`.

```python
import math

import numpy as np

from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES, TICKER_PARAMS

DEFAULT = list(SEED_PRICES)


def corr_matrix(tickers):
    """Rebuild the simulator's target correlation matrix for a ticker set."""
    n = len(tickers)
    C = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            r = GBMSimulator._pairwise_correlation(tickers[i], tickers[j])
            C[i, j] = C[j, i] = r
    return C


# --- 1. Positive-definiteness across ticker sets -----------------------------
cases = [
    ("default 10",           DEFAULT),
    ("tech only",            ["AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"]),
    ("default + 20 unknown", DEFAULT + [f"X{i}" for i in range(20)]),
    ("finance + unknowns",   ["JPM", "V"] + [f"Y{i}" for i in range(10)]),
]
for name, tk in cases:
    C = corr_matrix(tk)
    min_eig = np.linalg.eigvalsh(C).min()
    try:
        np.linalg.cholesky(C)
        status = "OK"
    except np.linalg.LinAlgError as exc:
        status = f"FAIL {exc}"
    print(f"{name:24s} n={len(tk):3d} min_eig={min_eig:+.4f} cholesky={status}")

# --- 2. Time step and per-tick magnitudes ------------------------------------
dt = GBMSimulator.DEFAULT_DT
print(f"\nDEFAULT_DT = {dt:.6e}   trading seconds/yr = {GBMSimulator.TRADING_SECONDS_PER_YEAR:,.0f}")
for t in ["AAPL", "TSLA", "V"]:
    sigma = TICKER_PARAMS[t]["sigma"]
    seed = SEED_PRICES[t]
    tick_sd = sigma * math.sqrt(dt)
    print(f"{t}: sigma={sigma}  per-tick log-return sd={tick_sd:.3e} "
          f"(~${tick_sd * seed:.4f} on ${seed})  daily sd={sigma / math.sqrt(252) * 100:.2f}%")

# --- 3. One simulated trading day, shocks disabled ---------------------------
np.random.seed(0)
sim = GBMSimulator(DEFAULT, event_probability=0.0)
start = {t: sim.get_price(t) for t in DEFAULT}
ticks = int(6.5 * 3600 / 0.5)          # 46,800 ticks = one 6.5-hour session
for _ in range(ticks):
    sim.step()
print(f"\nAfter one simulated trading day ({ticks:,} ticks):")
for t in ["AAPL", "TSLA", "V"]:
    end = sim.get_price(t)
    print(f"  {t}: {start[t]:.2f} -> {end:.2f}  ({(end / start[t] - 1) * 100:+.2f}%)")

# --- 4. Realised correlation and volatility vs targets -----------------------
np.random.seed(1)
sim2 = GBMSimulator(DEFAULT, event_probability=0.0)
prev = np.array([sim2.get_price(t) for t in DEFAULT])
rets = []
for _ in range(20_000):
    sim2.step()
    cur = np.array([sim2.get_price(t) for t in DEFAULT])
    rets.append(np.log(cur / prev))
    prev = cur
R = np.array(rets)
E = np.corrcoef(R.T)
ix = DEFAULT.index
print("\nRealised correlation (20k ticks):")
for a, b, target in [("AAPL", "MSFT", 0.6), ("JPM", "V", 0.5),
                     ("TSLA", "AAPL", 0.3), ("AAPL", "JPM", 0.3)]:
    print(f"  {a}/{b}: {E[ix(a), ix(b)]:.2f}  (target {target})")

print("Realised annualised sigma vs target:")
ann = R.std(axis=0) / math.sqrt(dt)
for t in ["AAPL", "TSLA", "V", "NVDA"]:
    print(f"  {t}: {ann[ix(t)]:.3f} vs {TICKER_PARAMS[t]['sigma']:.3f}")
```

**Output obtained on the current implementation** (Python 3.12, NumPy 2.x):

```
default 10               n= 10 min_eig=+0.4000 cholesky=OK
tech only                n=  7 min_eig=+0.4000 cholesky=OK
default + 20 unknown     n= 30 min_eig=+0.4000 cholesky=OK
finance + unknowns       n= 12 min_eig=+0.5000 cholesky=OK

DEFAULT_DT = 8.479175e-08   trading seconds/yr = 5,896,800
AAPL: sigma=0.22  per-tick log-return sd=6.406e-05 (~$0.0122 on $190.0)  daily sd=1.39%
TSLA: sigma=0.5  per-tick log-return sd=1.456e-04 (~$0.0364 on $250.0)  daily sd=3.15%
V: sigma=0.17  per-tick log-return sd=4.950e-05 (~$0.0139 on $280.0)  daily sd=1.07%

After one simulated trading day (46,800 ticks):
  AAPL: 190.00 -> 187.07  (-1.54%)
  TSLA: 250.00 -> 242.47  (-3.01%)
  V: 280.00 -> 284.71  (+1.68%)

Realised correlation (20k ticks):
  AAPL/MSFT: 0.60  (target 0.6)
  JPM/V: 0.50  (target 0.5)
  TSLA/AAPL: 0.30  (target 0.3)
  AAPL/JPM: 0.30  (target 0.3)
Realised annualised sigma vs target:
  AAPL: 0.221 vs 0.220
  TSLA: 0.501 vs 0.500
  V: 0.169 vs 0.170
  NVDA: 0.399 vs 0.400
```

The realised statistics match the targets to within sampling error, confirming that the Cholesky construction and the `dt` scaling are both correct. Note that §3 uses `sim.get_price()`, which returns the unrounded internal price — reading rounded prices would inject cent-level noise into a per-tick standard deviation of about the same size.

---

## Appendix B — Consuming the stream from the frontend

The client side of the contract, for reference by the frontend agent:

```typescript
// One EventSource for the whole app; every frame carries the full price map.
const source = new EventSource("/api/stream/prices");

source.onmessage = (event) => {
  const prices: Record<string, PriceUpdate> = JSON.parse(event.data);
  for (const [ticker, update] of Object.entries(prices)) {
    applyPrice(ticker, update);        // flash green/red on update.direction
    appendToSparkline(ticker, update.price, update.timestamp);
  }
  setConnectionStatus("connected");
};

source.onerror = () => {
  // EventSource reconnects on its own after the server's `retry: 1000`.
  setConnectionStatus(source.readyState === EventSource.CLOSED ? "disconnected" : "reconnecting");
};
```

Three things follow from the design above:

- **No reconnect logic is needed.** `EventSource` retries automatically using the server's `retry: 1000` directive [13, 14]; `onerror` is for the status indicator only.
- **Frames are complete, not deltas** — replace state, do not merge. A ticker missing from a frame has been removed from the watchlist.
- **Frame cadence depends on the source.** ~2/second on the simulator, ~1 per 15 s on Massive. The UI must not treat a gap between frames as a disconnection; use `EventSource.readyState`, not a frame timer.

---

## References

**Model and mathematics**

1. Black, F. and Scholes, M. (1973). "The Pricing of Options and Corporate Liabilities." *Journal of Political Economy*, 81(3), 637–654. https://doi.org/10.1086/260062 — the option-pricing model that assumes the GBM price dynamics used in §7.1.
2. Merton, R. C. (1973). "Theory of Rational Option Pricing." *Bell Journal of Economics and Management Science*, 4(1), 141–183. https://doi.org/10.2307/3003143
3. Øksendal, B. (2003). *Stochastic Differential Equations: An Introduction with Applications*, 6th ed. Springer. https://doi.org/10.1007/978-3-642-14394-6 — Itô's lemma and the closed-form solution of the GBM SDE.
4. Glasserman, P. (2004). *Monte Carlo Methods in Financial Engineering*. Springer, Ch. 2 (generating multivariate normal vectors via the Cholesky factorisation) and Ch. 3 (simulating GBM paths). https://doi.org/10.1007/978-0-387-21617-1
5. NumPy documentation — `numpy.linalg.cholesky`. https://numpy.org/doc/stable/reference/generated/numpy.linalg.cholesky.html (raises `LinAlgError` if the input is not positive definite).
6. Merton, R. C. (1976). "Option pricing when underlying stock returns are discontinuous." *Journal of Financial Economics*, 3(1–2), 125–144. https://doi.org/10.1016/0304-405X(76)90022-2 — background for the shock-event mechanism in §7.4.

**Market data provider**

7. Massive — official Python client library (`massive`). https://github.com/massive-com/client-python
8. Massive — API documentation home. https://massive.com/docs
9. Massive Knowledge Base — "What is the request limit for Massive's RESTful APIs?" https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis (free tier: 5 requests/minute).
10. Massive — Stocks REST API, Full Market Snapshot. https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot
11. Massive Knowledge Base — "What is the max number of tickers I can pass through Massive's Snapshot?" https://massive.com/knowledge-base/article/what-is-the-max-number-of-tickers-i-can-pass-through-massives-snapshot (unified snapshot: 250 symbols maximum).

**Platform and protocol**

12. Python documentation — `asyncio.to_thread`. https://docs.python.org/3/library/asyncio-task.html#asyncio.to_thread
13. WHATWG HTML Living Standard — Server-sent events (event stream format, the `retry` field, reconnection). https://html.spec.whatwg.org/multipage/server-sent-events.html
14. MDN Web Docs — `EventSource`. https://developer.mozilla.org/en-US/docs/Web/API/EventSource
15. Starlette documentation — Requests and `StreamingResponse` (`request.is_disconnected()`). https://www.starlette.io/requests/
16. FastAPI documentation — Lifespan Events. https://fastapi.tiangolo.com/advanced/events/
17. PEP 703 — Making the Global Interpreter Lock Optional in CPython. https://peps.python.org/pep-0703/

**Project documents**

- `planning/PLAN.md` — overall FinAlly specification.
- `planning/MARKET_DATA_SUMMARY.md` — implementation status summary.
- `planning/archive/` — earlier working notes: `MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, `MASSIVE_API.md`, `MARKET_DATA_REVIEW.md`, and the previous revision of this design.
