# Market Data Backend — Detailed Design

**Component:** `backend/app/market/`
**Status:** implemented and tested (see `planning/MARKET_DATA_SUMMARY.md`); this document is the design of record and the specification for the remaining integration work.
**Audience:** the Backend, Frontend, and Test agents building the rest of FinAlly on top of this layer.

---

## 0. How to read this document

Every section marked **[built]** describes code that exists today in `backend/app/market/` — the snippets are the real implementation with docstrings trimmed and the odd call reflowed to fit; `backend/app/market/` is the authority if the two ever drift. Sections marked **[to build]** specify code that the Backend agent still has to write (app lifecycle wiring, watchlist coordination, the day-change extension). Sections marked **[backlog]** are hardening items that are safe to defer.

Nothing in here contradicts `PLAN.md`; where `PLAN.md` §13 raised an open question about market data, §13 of this document answers it, and that answer is binding for downstream agents.

### Table of contents

1. [Design goals and constraints](#1-design-goals-and-constraints)
2. [Architecture](#2-architecture)
3. [File layout](#3-file-layout)
4. [Data model — `models.py`](#4-data-model--modelspy)
5. [Price cache — `cache.py`](#5-price-cache--cachepy)
6. [Unified interface — `interface.py`](#6-unified-interface--interfacepy)
7. [Seed data and parameters — `seed_prices.py`](#7-seed-data-and-parameters--seed_pricespy)
8. [The emulator — `simulator.py`](#8-the-emulator--simulatorpy)
9. [Massive API client — `massive_client.py`](#9-massive-api-client--massive_clientpy)
10. [Factory and configuration — `factory.py`](#10-factory-and-configuration--factorypy)
11. [SSE streaming — `stream.py`](#11-sse-streaming--streampy)
12. [Application integration](#12-application-integration-to-build)
13. [Decisions on open questions from PLAN §13](#13-decisions-on-open-questions-from-plan-13)
14. [Day-change extension](#14-day-change-extension-to-build)
15. [Testing strategy](#15-testing-strategy)
16. [Error handling and edge cases](#16-error-handling-and-edge-cases)
17. [Hardening backlog](#17-hardening-backlog)
18. [Quick reference](#18-quick-reference)

---

## 1. Design goals and constraints

| Goal | How it is met |
|---|---|
| The app runs with no API key at all | Simulator is the default; `massive` is only *called* when `MASSIVE_API_KEY` is set |
| Downstream code never knows where prices came from | One ABC (`MarketDataSource`), one data type (`PriceUpdate`), one read surface (`PriceCache`) |
| Prices feel alive on screen | 500 ms ticks, per-ticker volatility calibrated so a tick moves ~1–9 ¢, correlated sector moves, occasional shocks |
| Real data must fit a 5 req/min free tier | One batched snapshot call per poll covers *all* tickers; 15 s interval = 4 calls/min |
| No blocking of the event loop | The synchronous `massive` REST client runs under `asyncio.to_thread` |
| A dead data source must not take down the app | Every background loop catches, logs, and continues |
| The stream must survive a tab sleeping, a laptop lid, a proxy | SSE with a `retry:` directive; the browser's `EventSource` reconnects on its own |

Non-goals, stated so nobody designs for them: no order book, no historical bar storage, no per-user data isolation (single-user, `user_id="default"`), no tick persistence — the cache is in-memory and rebuilt from seed prices on restart.

---

## 2. Architecture

```
                     MASSIVE_API_KEY?
                            │
              ┌─────────────┴─────────────┐
              │ create_market_data_source │        factory.py
              └─────────────┬─────────────┘
          unset │                        │ set
                ▼                        ▼
    ┌───────────────────────┐  ┌────────────────────────┐
    │ SimulatorDataSource   │  │ MassiveDataSource      │
    │  GBMSimulator.step()  │  │  get_snapshot_all()    │
    │  every 500 ms         │  │  every 15 s (1 call)   │
    └───────────┬───────────┘  └───────────┬────────────┘
                │      both implement      │
                │      MarketDataSource    │
                └────────────┬─────────────┘
                             ▼  cache.update(ticker, price, ts)
                    ┌──────────────────┐
                    │   PriceCache     │  in-memory, Lock-guarded,
                    │  {ticker: Price  │  monotonic `version` counter
                    │        Update}   │
                    └───┬────┬─────┬───┘
         get_all()      │    │     │   get_price()
                        ▼    ▼     ▼
          SSE /api/stream/prices   trade execution
                        │          portfolio valuation
                        ▼          snapshot task
                   EventSource (browser)
```

Three rules keep this decoupled, and they are worth stating explicitly because every later section depends on them:

1. **A data source never returns a price.** It pushes into the cache on its own schedule. Callers that need "the price right now" ask the cache, never the source. This is what makes the simulator and the poller substitutable even though one produces data 30× more often than the other.
2. **The cache is the only shared mutable state.** It is guarded by a `threading.Lock` rather than an `asyncio.Lock` because the Massive client writes from a worker thread.
3. **`PriceUpdate` is the only type that crosses the boundary.** SSE serialization, portfolio math, and trade fills all consume it; nothing downstream imports `GBMSimulator` or `RESTClient`.

---

## 3. File layout

```
backend/app/market/
├── __init__.py          # public API re-exports
├── models.py            # PriceUpdate
├── cache.py             # PriceCache
├── interface.py         # MarketDataSource (ABC)
├── seed_prices.py       # SEED_PRICES, TICKER_PARAMS, correlation constants
├── simulator.py         # GBMSimulator + SimulatorDataSource
├── massive_client.py    # MassiveDataSource
├── factory.py           # create_market_data_source()
└── stream.py            # create_stream_router() → GET /api/stream/prices
```

**[built]** `__init__.py` is the import surface for the rest of the backend. Reaching into submodules from outside `app.market` is a smell:

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

---

## 4. Data model — `models.py`

**[built]**

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

### Why it looks like this

- **`frozen=True`** — an update handed to the SSE generator, the portfolio valuer, and a trade handler is the same object; immutability means none of them can corrupt the others' view. It also makes the object hashable and safe to stash in a list for debugging.
- **`slots=True`** — no `__dict__` per instance. At 2 updates/second × 10+ tickers this matters less than it sounds, but it is free.
- **`change` / `change_percent` / `direction` are computed properties, not stored fields.** They are pure functions of `price` and `previous_price`, so storing them would create a way for the object to be internally inconsistent. The rounding (4 dp) exists so that floating-point noise never reaches the wire as `-2.8421709430404007e-14`.
- **`previous_price` means "the price at the previous update", not "yesterday's close".** This is the tick-to-tick delta that drives the green/red flash. Day-over-day change is a separate concept — see §14.
- **`timestamp` is Unix *seconds* as a float.** Massive returns milliseconds and the client divides; the simulator uses `time.time()`. Everything downstream can assume seconds.

### Example

```python
>>> u = PriceUpdate(ticker="AAPL", price=190.25, previous_price=190.00, timestamp=1758585600.0)
>>> u.change, u.change_percent, u.direction
(0.25, 0.1316, 'up')
>>> u.to_dict()["direction"]
'up'
>>> u.price = 191.0
dataclasses.FrozenInstanceError: cannot assign to field 'price'
```

---

## 5. Price cache — `cache.py`

**[built]**

```python
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
        """Snapshot of all current prices. Returns a shallow copy."""
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
        """Current version counter. Useful for SSE change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Three decisions worth defending

**Rounding happens here, once.** `update()` rounds to 2 dp on the way in, so every consumer — the SSE payload, the trade fill price, the portfolio valuation — sees exactly the same number the user sees on screen. If rounding were deferred to display, a user could buy 10 shares at a displayed $190.25 and see cash decrease by $1902.4999999. The simulator keeps *unrounded* prices internally (§8) so that sub-cent drift still accumulates; only the published value is rounded.

**First update for a ticker is `flat`.** `previous_price` falls back to `price` when there is no prior entry, so a newly-added ticker renders with no flash rather than a bogus 100% move.

**The version counter is the change-detection primitive.** It is a monotonically increasing integer bumped on every write. The SSE generator holds the last version it sent and skips the serialize-and-send when nothing has moved (§11). An alternative design — having the data source push into a per-client `asyncio.Queue` — would be more "correct" event-driven architecture, but it couples the producer to the consumer set, needs backpressure handling for a slow client, and buys nothing at 10 tickers. The counter gets the same saved bandwidth in one integer comparison.

### Thread-safety model

| Writer | Runs on | Lock needed? |
|---|---|---|
| `SimulatorDataSource._run_loop` | event loop (coroutine) | yes — coexists with reader coroutines only, but see next row |
| `MassiveDataSource._poll_once` | event loop, but the HTTP call is in a `to_thread` worker; the `update()` call itself is back on the loop | yes |
| SSE generator, trade routes, snapshot task | event loop | read-only |

Today all `update()` calls land on the event loop, so a lock is not strictly required. It is there because (a) it costs a handful of nanoseconds on an uncontended CPython lock, and (b) the moment someone moves the cache write *inside* the `to_thread` worker — an obvious optimization a future agent might make — the unlocked version becomes a genuine race. Design for that edit.

### Examples

```python
cache = PriceCache()

cache.update("AAPL", 190.00)           # first write  → direction 'flat'
u = cache.update("AAPL", 190.25)       # second write → direction 'up'
u.change                                # 0.25

cache.get_price("AAPL")                 # 190.25
cache.get_price("NOPE")                 # None       ← always handle this
len(cache)                              # 1
"AAPL" in cache                         # True

v0 = cache.version
cache.update("AAPL", 190.30)
cache.version > v0                      # True — something moved
```

---

## 6. Unified interface — `interface.py`

**[built]**

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
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. No-op if not present.

        Also removes the ticker from the PriceCache.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

### The contract both implementations must honour

| Guarantee | Simulator | Massive |
|---|---|---|
| After `start(tickers)` returns, every ticker has a price in the cache | seeds the cache synchronously from `SEED_PRICES` | performs one blocking poll before returning |
| `add_ticker` is idempotent | guarded by `if ticker in self._prices: return` | guarded by `if ticker not in self._tickers` |
| `remove_ticker` also evicts from the cache | yes | yes |
| `stop()` is idempotent and awaits task cancellation | yes | yes |
| A failing cycle never kills the loop | `except Exception: logger.exception(...)` | `except Exception: logger.error(...)` |
| `get_tickers()` is sync (callable from a route without awaiting) | yes | yes |

`start()` being *effectively synchronous* for the first price is the reason the frontend never renders an empty watchlist: by the time FastAPI accepts its first request, the cache is populated.

Note the asymmetry the interface deliberately hides: `add_ticker` on the simulator makes a price available immediately, whereas on Massive the ticker has no price until the next poll (up to 15 s later). §12 shows the `ensure_tracked` helper that papers over this for callers who need a price *now* (trade execution).

---

## 7. Seed data and parameters — `seed_prices.py`

**[built]**

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00, "TSLA": 250.00,
    "NVDA": 800.00, "META": 500.00, "JPM": 195.00,  "V": 280.00,    "NFLX": 600.00,
}

# sigma: annualized volatility (higher = more price movement)
# mu: annualized drift / expected return
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

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6     # Tech stocks move together
INTRA_FINANCE_CORR = 0.5  # Finance stocks move together
CROSS_GROUP_CORR = 0.3    # Between sectors / unknown tickers
TSLA_CORR = 0.3           # TSLA does its own thing
```

These are constants only — no logic — so the file can be imported by tests, the demo script, and any future admin endpoint without side effects. A ticker not in `TICKER_PARAMS` gets `DEFAULT_PARAMS`; a ticker not in `SEED_PRICES` gets a uniform random price in $50–$300 (§8).

---

## 8. The emulator — `simulator.py`

### 8.1 The maths

Each tick advances every tracked price by one step of geometric Brownian motion:

```
S(t+dt) = S(t) · exp( (mu − sigma²/2)·dt  +  sigma·√dt·Z )
```

- `mu` — annualized drift; `sigma` — annualized volatility; both per ticker from `TICKER_PARAMS`.
- `dt` — the tick expressed as a fraction of a *trading* year: `0.5 / (252 × 6.5 × 3600) ≈ 8.479e-8`.
- `Z` — a standard normal draw, **correlated across tickers** (§8.3).

GBM is the right model here for three reasons: prices stay strictly positive (it is multiplicative and `exp` is positive), log-returns are normal so the price distribution is lognormal like real equities, and the `−sigma²/2` Itô correction means the *expected* price grows at exactly `mu` rather than `mu + sigma²/2`.

### 8.2 Calibration — does it look real?

With the parameters above, one tick moves a price by (1 sigma):

| Ticker | sigma | seed | 1σ per 500 ms tick | 1σ per 6.5 h day |
|---|---|---|---|---|
| AAPL | 0.22 | $190 | 0.0064 % = **$0.012** | 1.39 % = $2.63 |
| MSFT | 0.20 | $420 | 0.0058 % = **$0.025** | 1.26 % = $5.29 |
| V | 0.17 | $280 | 0.0050 % = **$0.014** | 1.07 % = $3.00 |
| TSLA | 0.50 | $250 | 0.0146 % = **$0.036** | 3.15 % = $7.87 |
| NVDA | 0.40 | $800 | 0.0116 % = **$0.093** | 2.52 % = $20.16 |

Two things fall out of this table and both are intentional:

- A typical tick is **one to nine cents** — large enough that the price flash fires most ticks, small enough that the number does not jitter distractingly. Cheap, low-vol names (AAPL, V) round to flat on a fair fraction of ticks, which reads as realistic rather than broken.
- The daily 1σ range matches the real-world figure implied by the annualized vol (`sigma·√(1/252)`), so a user leaving the tab open for an hour sees a plausible intraday range rather than a random walk to zero or to the moon.

There are 46,800 ticks in a simulated 6.5-hour session, so the frontend sparkline (which accumulates from the SSE stream since page load) fills its window within a minute or two of watching.

### 8.3 Correlated moves via Cholesky

Independent draws would show ten tickers wandering in unrelated directions — nothing like a real market where sector news moves a whole group. So the simulator builds a correlation matrix `C` from sector membership, factors it as `C = L·Lᵀ` (Cholesky), and transforms independent normals: `Z_corr = L @ Z_indep`. The result has exactly the covariance structure of `C`.

Pairwise rule, in priority order: TSLA with anything → 0.3; two tech names → 0.6; two finance names → 0.5; everything else → 0.3. Cholesky also acts as a validity check — it raises `LinAlgError` if the matrix is not positive-definite, which is how a bad correlation table would be caught immediately rather than producing subtly wrong dynamics.

### 8.4 Shock events

Every tick, every ticker has a 0.1 % chance of a 2–5 % jump in a random direction. Arithmetic: 0.001 × 10 tickers × 2 ticks/s → **one shock somewhere about every 50 seconds**, and about every 500 seconds for any given ticker. That is frequent enough to keep a demo interesting and rare enough that a user watching one name mostly sees ordinary drift.

### 8.5 `GBMSimulator` — the engine **[built]**

```python
class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices."""

    # 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800 seconds
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
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

        z_independent = np.random.standard_normal(n)
        if self._cholesky is not None:
            z_correlated = self._cholesky @ z_independent
        else:
            z_correlated = z_independent

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
                logger.debug("Random event on %s: %.1f%% %s", ticker,
                             shock_magnitude * 100, "up" if shock_sign > 0 else "down")

            result[ticker] = round(self._prices[ticker], 2)

        return result

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

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

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
        """Rebuild the Cholesky decomposition. O(n^2) but n < 50."""
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

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        # TSLA is in the tech set but behaves independently
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

Implementation notes that are easy to get wrong:

- **`self._prices` holds unrounded floats**; only the returned dict is rounded. Rounding the internal state would quantize the random walk and, over tens of thousands of ticks, bias it.
- **`_add_ticker_internal` vs `add_ticker`** — construction adds N tickers then factors once (O(n²) total) instead of factoring after each insert (O(n³)).
- **`dict(DEFAULT_PARAMS)`** copies the default so a later mutation of one ticker's params cannot leak into every other unknown ticker.
- **`_pairwise_correlation` is a `@staticmethod`** — pure, trivially testable, no instance state.

### 8.6 `SimulatorDataSource` — the async wrapper **[built]**

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

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

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

The `try` sits *inside* the `while`, wrapping only the work — never the `sleep`. An exception therefore costs one tick, not the whole stream. `await self._task` after `cancel()` is what makes `stop()` deterministic: when it returns, the loop has actually finished, so no stray `update()` can land after shutdown.

### 8.7 Running it

```python
import asyncio
from app.market import PriceCache
from app.market.simulator import SimulatorDataSource

async def main():
    cache = PriceCache()
    source = SimulatorDataSource(cache, update_interval=0.5)
    await source.start(["AAPL", "NVDA", "TSLA"])

    for _ in range(5):
        await asyncio.sleep(1)
        for t, u in sorted(cache.get_all().items()):
            print(f"{t:<6} {u.price:>8.2f}  {u.change:+.2f}  {u.direction}")

    await source.add_ticker("PYPL")   # priced immediately
    await source.stop()

asyncio.run(main())
```

A richer version of exactly this — a live Rich dashboard with sparklines and an event log — already exists at `backend/market_data_demo.py` (`uv run market_data_demo.py`). Use it to eyeball parameter changes before committing them.

---

## 9. Massive API client — `massive_client.py`

### 9.1 The one endpoint that matters

`GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,...` — the **batched** snapshot. It returns every requested ticker in a *single* HTTP request, which is the whole reason the free tier works:

| Tier | Limit | Poll interval | Calls/min | Headroom |
|---|---|---|---|---|
| Free | 5 req/min | 15 s (default) | 4 | 1 call spare |
| Paid | effectively unlimited | 2–5 s | 12–30 | ample |

Polling per ticker instead would cost 10 calls per cycle and blow the free tier on the first poll. If a future change needs per-ticker detail (e.g. bid/ask on the selected ticker), it must be an *additional*, user-triggered call — never part of the loop.

Field mapping from the snapshot response into `PriceUpdate`:

| Massive field | Used for | Note |
|---|---|---|
| `snap.ticker` | cache key | already uppercase |
| `snap.last_trade.price` | `price` | the tradeable/displayable price |
| `snap.last_trade.timestamp` | `timestamp` | **Unix milliseconds** → divide by 1000 |
| `snap.day.previous_close` | day change (§14) | not consumed yet |
| `snap.day.change_percent` | day change (§14) | not consumed yet |

### 9.2 Implementation **[built]**

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Rate limits:
      - Free tier: 5 req/min → poll every 15s (default)
      - Paid tiers: higher limits → poll every 2-5s
    """

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

        # Do an immediate first poll so the cache has data right away
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info("Massive poller started: %d tickers, %.1fs interval",
                    len(tickers), self._interval)

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
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

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
                    logger.warning("Skipping snapshot for %s: %s",
                                   getattr(snap, "ticker", "???"), e)
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — the loop will retry on the next interval.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.
```

### 9.3 Why these details matter

**`asyncio.to_thread` around the client call.** `massive.RESTClient` is synchronous `requests`-style I/O. Calling it directly on the event loop would freeze *every* SSE connection for the duration of the HTTP round trip — a visible stall in every browser tab, every 15 seconds. The thread hop costs microseconds and buys a fully responsive loop.

**Two nested `try` blocks, deliberately.** The inner one is per-snapshot: a single ticker with a null `last_trade` (a halted symbol, a bad ticker the user added, a pre-market name that has not traded) is logged and skipped while the other nine still update. The outer one is per-poll: an auth failure, a 429, or a dropped connection logs and returns, and the loop sleeps and tries again. There is no failure mode in which one bad ticker or one bad response stops the stream.

**The immediate poll in `start()`.** Without it, a Massive-backed app would serve an empty watchlist for up to 15 seconds after boot. Note this makes `start()` do real network I/O — it can take a second, and if the key is invalid it logs an error and continues with an empty cache rather than raising. That is the right trade: a bad key should degrade the app, not prevent it from booting.

**`ticker.upper().strip()` in add/remove.** Massive is case-sensitive on the query string and the cache is keyed by the string the API returns (uppercase). Normalizing at the boundary keeps `"aapl"` from creating a phantom entry that never receives updates. §13 pushes this normalization further up, into a shared validator, so the simulator path behaves identically.

**Module-level imports of `massive`.** An earlier revision imported lazily inside methods so that the package would be optional. It is a declared core dependency in `pyproject.toml`, so the lazy import bought nothing and broke `patch("app.market.massive_client.RESTClient")` in tests. Top-level import, patchable name.

### 9.4 Behaviour outside market hours

The snapshot endpoint keeps returning the last traded price when the market is closed, with a `last_trade.timestamp` that stops advancing. The cache therefore writes the same price repeatedly: `previous_price == price`, `direction == "flat"`, no flash. Because `update()` still bumps `version` on each identical write, the SSE stream re-sends the payload every 15 s — harmless, and it keeps the connection warm. The simulator has no such notion of hours; it runs continuously, which is what you want for a demo.

---

## 10. Factory and configuration — `factory.py`

**[built]**

```python
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

`.strip()` is load-bearing: `MASSIVE_API_KEY=` and `MASSIVE_API_KEY="   "` in a `.env` file both mean "not configured", and without the strip the second would select the Massive path and then 401 on every poll. The factory logs which source it chose at INFO — that one line is the first thing to check when someone reports "prices aren't moving".

The factory returns an **unstarted** source; the caller owns `start()`/`stop()`. This keeps construction synchronous and testable, and puts lifecycle control in the one place that knows the app's lifespan (§12).

| Variable | Effect on this subsystem |
|---|---|
| `MASSIVE_API_KEY` unset/empty | GBM simulator, 500 ms ticks, no network |
| `MASSIVE_API_KEY` set | Massive REST poller, 15 s default interval |

Poll and tick intervals are constructor arguments, not env vars, today. If they need to become configurable (a paid-tier user wanting 2 s polling), add `MARKET_POLL_INTERVAL` here in the factory rather than reading `os.environ` inside the data sources — the sources stay pure and testable, and all env coupling stays in one file.

---

## 11. SSE streaming — `stream.py`

### 11.1 Implementation **[built]**

```python
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
                    yield f"data: {json.dumps(data)}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### 11.2 Cadence: what actually goes on the wire

`PLAN.md` §6 says the server pushes "at a regular cadence (~500 ms)". The precise behaviour is **poll the cache every 500 ms, send only if the version counter moved**:

- **Simulator**: the version bumps 10+ times per tick, so in practice a payload goes out every 500 ms. Regular spacing is what the frontend sparklines want.
- **Massive**: the version only moves after a poll, so the client receives roughly one payload every 15 s and nothing in between.
- **Idle/empty cache**: nothing is sent at all.

Each payload is the **complete price map**, not a delta. At ~10–30 tickers that is 1–4 KB of JSON twice a second — trivial — and it makes the client stateless: a browser that reconnects after a sleep gets a full, correct picture on the first event with no resync logic.

### 11.3 Wire format

```
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 190.25, "previous_price": 190.13, "timestamp": 1758585600.12, "change": 0.12, "change_percent": 0.0631, "direction": "up"}, "NVDA": {...}}

data: {"AAPL": {...}, "NVDA": {...}}
```

Contract for the frontend: unnamed events (so `onmessage` fires, no `addEventListener` needed), `data` is a JSON object keyed by ticker, every value is the `PriceUpdate.to_dict()` shape above, and the map is authoritative — a ticker absent from the payload is no longer tracked.

### 11.4 Client example **[to build — Frontend agent]**

```ts
type PriceTick = {
  ticker: string; price: number; previous_price: number;
  timestamp: number; change: number; change_percent: number;
  direction: "up" | "down" | "flat";
};

const es = new EventSource("/api/stream/prices");

es.onmessage = (e) => {
  const prices: Record<string, PriceTick> = JSON.parse(e.data);
  applyPrices(prices);                 // update grid, flash cells, push to sparkline buffers
  setConnectionStatus("connected");    // green dot
};

es.onerror = () => {
  // EventSource reconnects automatically after the server's retry: 1000
  setConnectionStatus(es.readyState === EventSource.CLOSED ? "disconnected" : "reconnecting");
};
```

The header's connection dot maps directly to `EventSource.readyState`: `OPEN` → green, `CONNECTING` → yellow, `CLOSED` → red.

### 11.5 Why poll-and-compare rather than pub/sub

A queue-per-client design would push events the instant a price changes and drop the 500 ms poll. It also adds: a subscriber registry on the cache, fan-out on every one of ~20 writes per second, per-client backpressure policy when a slow browser cannot keep up, and cleanup on disconnect. The version counter achieves the same saved bandwidth with an integer comparison, bounds the send rate by construction (a client can never be flooded faster than 2 payloads/second), and keeps the cache free of consumer knowledge. Revisit only if tick rate or client count grows by an order of magnitude.

### 11.6 Disconnect handling

`request.is_disconnected()` is checked at the top of each iteration, so a closed tab ends the generator within ~500 ms instead of leaking a task. The `except asyncio.CancelledError` arm covers server shutdown, where Uvicorn cancels in-flight tasks. Both paths log, which makes connection churn visible during E2E debugging.

---

## 12. Application integration **[to build]**

This is the contract for the Backend agent. The market layer is complete; what follows is how the rest of the app must drive it.

### 12.1 Lifespan wiring

```python
# backend/app/main.py
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.market import (
    MarketDataSource,
    PriceCache,
    create_market_data_source,
    create_stream_router,
)
from app.db import init_db, load_tracked_tickers   # backend agent's modules

# Module scope so the router and the lifespan share one instance.
price_cache = PriceCache()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Database first — the watchlist lives there
    await init_db()                                   # lazy create + seed (PLAN §7)

    app.state.price_cache = price_cache

    # 2. Data source chosen by env, started on the tracked set (§12.2)
    source = create_market_data_source(price_cache)
    app.state.market_source = source
    await source.start(await load_tracked_tickers())

    # 3. Background portfolio snapshots every 30s (PLAN §7)
    snapshot_task = asyncio.create_task(snapshot_loop(price_cache), name="portfolio-snapshots")

    yield                                             # --- app is serving ---

    snapshot_task.cancel()
    await source.stop()                               # awaits loop termination


app = FastAPI(title="FinAlly", lifespan=lifespan)
app.include_router(create_stream_router(price_cache))


def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_market_source(request: Request) -> MarketDataSource:
    return request.app.state.market_source
```

Three ordering constraints, all load-bearing:

- **The cache is created at module scope**, because `create_stream_router()` runs at import time while `app.state` is only populated inside the lifespan. Both must reference the same object or the SSE endpoint streams an empty cache forever.
- **`init_db()` precedes `source.start()`**, because the tracked ticker set is read from SQLite.
- **`source.stop()` is awaited on shutdown**, so no background write can land after the app has torn down.

Dependencies take `request` rather than closing over a module global so that tests can build an app around their own cache.

### 12.2 The tracked ticker set: watchlist ∪ open positions

The set of tickers the data source tracks is **not** the watchlist — it is the watchlist *plus every ticker with an open position*. Otherwise a user who removes a held ticker from their watchlist loses its price, and their portfolio valuation silently freezes or errors.

```python
async def load_tracked_tickers() -> list[str]:
    """Watchlist ∪ tickers with a non-zero position."""
    watch = await db.fetch_watchlist_tickers()          # SELECT ticker FROM watchlist
    held = await db.fetch_position_tickers()            # SELECT ticker FROM positions WHERE quantity > 0
    return sorted(set(watch) | set(held))
```

### 12.3 Watchlist add/remove

```python
@router.post("/api/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
    cache: PriceCache = Depends(get_price_cache),
):
    ticker = normalize_ticker(payload.ticker)           # §13.4
    await db.insert_watchlist(ticker)                   # UNIQUE(user_id, ticker) → idempotent
    await source.add_ticker(ticker)
    return {"ticker": ticker, "price": cache.get_price(ticker)}   # price may be None on Massive


@router.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    ticker = normalize_ticker(ticker)
    await db.delete_watchlist(ticker)

    # Keep streaming prices for anything we still hold
    if not await db.has_open_position(ticker):
        await source.remove_ticker(ticker)
    return {"status": "ok", "ticker": ticker}
```

The LLM's `watchlist_changes` actions (PLAN §9) must go through these same functions, not straight to SQL — otherwise AI-driven changes never reach the data source.

### 12.4 Trading a ticker, tracked or not

Trade execution needs a price *synchronously*. `ensure_tracked` bridges the gap between "user typed a ticker into the trade bar" and "the cache has a price for it":

```python
async def ensure_tracked(
    ticker: str,
    source: MarketDataSource,
    cache: PriceCache,
    timeout: float = 20.0,
    poll: float = 0.25,
) -> float:
    """Return a live price for `ticker`, adding it to the data source if needed.

    Raises HTTPException(404) if no price materializes (unknown symbol on Massive).
    """
    price = cache.get_price(ticker)
    if price is not None:
        return price

    await source.add_ticker(ticker)

    # Simulator seeds immediately; Massive needs up to one poll interval.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        price = cache.get_price(ticker)
        if price is not None:
            return price
        await asyncio.sleep(poll)

    await source.remove_ticker(ticker)     # don't leave a dead ticker in the poll set
    raise HTTPException(404, f"No market data available for {ticker}")
```

```python
@router.post("/api/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    source: MarketDataSource = Depends(get_market_source),
    cache: PriceCache = Depends(get_price_cache),
):
    ticker = normalize_ticker(trade.ticker)
    price = await ensure_tracked(ticker, source, cache)
    # ... validate cash/shares, write positions + trades, snapshot portfolio ...
```

On the simulator path this returns on the first check for a tracked ticker and after one `add_ticker` for a new one — no waiting. On the Massive path an unknown symbol costs up to one poll interval and then either resolves or 404s, which is also the ticker-validation mechanism for real data (§13.4).

### 12.5 Reading prices elsewhere

Portfolio valuation and the 30-second snapshot task read the cache directly and must treat a miss as "skip", never as zero:

```python
def portfolio_value(positions: list[Position], cash: float, cache: PriceCache) -> float:
    total = cash
    for pos in positions:
        price = cache.get_price(pos.ticker)
        if price is None:
            logger.warning("No price for held ticker %s; valuing at avg_cost", pos.ticker)
            price = pos.avg_cost          # last known good, never 0.0
        total += pos.quantity * price
    return round(total, 2)
```

Valuing a missing price at `0.0` would show a catastrophic fake loss on the heatmap and P&L chart the moment a poll fails. Falling back to `avg_cost` shows a flat position instead — wrong but not alarming, and self-correcting on the next successful update.

---

## 13. Decisions on open questions from PLAN §13

These resolve the market-data items flagged in the plan's review notes. They are binding for downstream agents.

### 13.1 Can the trade bar / LLM trade an unwatched ticker? — **Yes**

Any valid ticker is tradeable. `ensure_tracked` (§12.4) adds it to the data source on demand, so a price exists before the fill. The trade does *not* implicitly add it to the watchlist; it enters the tracked set because a position exists (§12.2). The frontend should still offer watchlist symbols as autocomplete suggestions — a convenience, not a constraint.

### 13.2 What happens to a position when its ticker leaves the watchlist? — **Keep tracking it**

Removal from the watchlist removes it from the watchlist *table and UI only*. `remove_ticker` is called only when there is no open position (§12.3). Tracked set = watchlist ∪ held. When the position is later closed, the ticker stops being tracked at the next app start; leaving it tracked until then costs one simulator column or nothing at all on a batched Massive call.

### 13.3 Sell to exactly zero — **Delete the position row**

A flat position disappears from the positions table and the heatmap. A later re-buy inserts a fresh row with a fresh `avg_cost`, which is the correct cost basis. The `trades` table retains the full history either way. (This is a portfolio-layer decision recorded here because it determines when a ticker leaves the tracked set.)

### 13.4 Ticker validation — **Normalize everywhere, validate by resolution**

```python
import re
from fastapi import HTTPException

TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")   # AAPL, V, BRK.B


def normalize_ticker(raw: str) -> str:
    """Uppercase, trim, and syntax-check a user- or LLM-supplied ticker."""
    ticker = (raw or "").strip().upper()
    if not TICKER_RE.match(ticker):
        raise HTTPException(422, f"Invalid ticker symbol: {raw!r}")
    return ticker
```

Every entry point — trade bar, watchlist POST/DELETE, LLM `trades` and `watchlist_changes` — calls this first. Beyond syntax, the two sources validate differently and that is acceptable:

- **Simulator**: any syntactically valid symbol is accepted and gets a random $50–$300 seed. There is no universe of "real" tickers to check against, and rejecting unknown symbols would make the demo feel broken.
- **Massive**: a nonexistent symbol simply never appears in the snapshot response, so `ensure_tracked` times out and returns 404 — real validation, for free, with no extra API call.

### 13.5 SSE cadence wording — **Documented as version-based**

§11.2 records what is actually built: poll the cache every 500 ms, send when the version changed. `PLAN.md` §6's "~500 ms cadence" is accurate for the simulator and misleading for Massive; treat §11.2 as authoritative.

### 13.6 Massive free-tier polling — **One batched call per cycle**

`get_snapshot_all(tickers=[...])` covers all tracked tickers in one request: 4 calls/min at the default 15 s interval against a 5 req/min limit (§9.1). Per-ticker polling is explicitly forbidden in the loop.

### 13.7 SQLite concurrency — **WAL, and market data does not write**

The market data layer touches no database at all: prices live in memory, and the SQLite writers are trade execution and the 30 s snapshot task. The Backend agent should still enable `PRAGMA journal_mode=WAL` at init as `PLAN.md` §13 recommends; that is a database-layer task, noted here only to make explicit that this subsystem adds no write contention.

---

## 14. Day-change extension **[to build]**

`PLAN.md` §10 asks the watchlist to show "daily change %". `PriceUpdate.change_percent` is tick-to-tick — a number near ±0.01 % — which is not that. Two small additions cover it without disturbing the existing contract.

**Cache a session base price per ticker:**

```python
class PriceCache:
    def __init__(self) -> None:
        ...
        self._session_base: dict[str, float] = {}   # previous close, or first price seen

    def set_session_base(self, ticker: str, base: float) -> None:
        with self._lock:
            self._session_base[ticker] = round(base, 2)

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        with self._lock:
            ...
            base = self._session_base.setdefault(ticker, round(price, 2))
            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
                session_base=base,          # new optional field, defaults to None
            )
            ...
```

**Expose it on the model:**

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ...
    session_base: float | None = None

    @property
    def day_change_percent(self) -> float:
        """Change vs. the session base (previous close, or first price seen)."""
        if not self.session_base:
            return 0.0
        return round((self.price - self.session_base) / self.session_base * 100, 2)
```

Add `"day_change_percent"` to `to_dict()` and the frontend gets it on every SSE event with no new endpoint.

Where the base comes from:

- **Massive** — `snap.day.previous_close`, set on every poll via `set_session_base()`. Correct by definition, and it resets naturally at the next session.
- **Simulator** — the seed price, i.e. the first price written at `start()`. `setdefault` in `update()` handles this with no simulator changes: the day change is measured from process start, which for a demo session is the right anchor.

Keep `session_base` optional with a `None` default so existing constructions and the 73 passing tests remain valid.

---

## 15. Testing strategy

73 tests currently pass across six modules in `backend/tests/market/` (84 % coverage). The patterns below are what new tests should follow.

### 15.1 Simulator — statistical properties, not exact values

A seeded RNG makes GBM deterministic, but asserting exact prices tests the RNG, not the model. Assert the properties that must hold:

```python
def test_prices_stay_positive_over_many_steps():
    sim = GBMSimulator(["TSLA"], event_probability=0.5)   # shock-heavy
    for _ in range(5_000):
        assert all(p > 0 for p in sim.step().values())


def test_realized_volatility_matches_sigma():
    """Over many ticks, realized log-return std should be near sigma*sqrt(dt)."""
    sim = GBMSimulator(["AAPL"], event_probability=0.0)   # no shocks
    prices = [sim.get_price("AAPL")]
    for _ in range(20_000):
        prices.append(sim.step()["AAPL"])

    rets = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
    realized = statistics.stdev(rets)
    expected = 0.22 * math.sqrt(GBMSimulator.DEFAULT_DT)
    assert 0.5 * expected < realized < 2.0 * expected      # wide band: rounding + sampling


def test_cholesky_builds_for_full_default_watchlist():
    sim = GBMSimulator(list(SEED_PRICES))                  # all 10 defaults
    assert sim._cholesky.shape == (10, 10)                 # raises LinAlgError if not PSD
    assert len(sim.step()) == 10


def test_add_remove_rebuilds_matrix():
    sim = GBMSimulator(["AAPL", "MSFT"])
    sim.add_ticker("PYPL")
    assert sim.get_tickers() == ["AAPL", "MSFT", "PYPL"]
    assert sim.get_price("PYPL") is not None               # random seed in $50-300
    sim.remove_ticker("AAPL")
    assert "AAPL" not in sim.get_tickers()
    assert len(sim.step()) == 2
```

Note the wide tolerance band: `step()` rounds its output to 2 dp, which adds quantization noise to log-returns of a $190 stock moving a cent at a time. A tight band here is a flaky test.

### 15.2 Cache — including the thread-safety claim

```python
def test_first_update_is_flat():
    cache = PriceCache()
    u = cache.update("AAPL", 190.0)
    assert u.previous_price == 190.0 and u.direction == "flat"


def test_version_increments_on_every_write():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.0)
    cache.update("AAPL", 190.0)          # same price still counts as a write
    assert cache.version == v0 + 2


def test_concurrent_writers_do_not_lose_updates():
    cache = PriceCache()
    def hammer(t):
        for i in range(1_000):
            cache.update(t, 100.0 + i / 100)

    threads = [Thread(target=hammer, args=(f"T{i}",)) for i in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert cache.version == 8_000
    assert len(cache) == 8
```

### 15.3 Massive — mock the client, never the network

```python
def _snapshot(ticker, price, ts_ms):
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = ts_ms
    return snap


async def test_poll_writes_cache_and_converts_ms_to_seconds():
    cache = PriceCache()
    source = MassiveDataSource(api_key="k", price_cache=cache)
    source._client = MagicMock()
    source._tickers = ["AAPL"]
    source._client.get_snapshot_all.return_value = [_snapshot("AAPL", 190.25, 1_675_190_399_000)]

    await source._poll_once()

    assert cache.get_price("AAPL") == 190.25
    assert cache.get("AAPL").timestamp == 1_675_190_399.0


async def test_malformed_snapshot_is_skipped_but_others_still_update():
    cache = PriceCache()
    source = MassiveDataSource(api_key="k", price_cache=cache)
    source._client = MagicMock()
    source._tickers = ["AAPL", "BAD"]
    bad = MagicMock(); bad.ticker = "BAD"; bad.last_trade = None
    source._client.get_snapshot_all.return_value = [_snapshot("AAPL", 190.0, 1_000), bad]

    await source._poll_once()          # must not raise

    assert cache.get_price("AAPL") == 190.0
    assert cache.get_price("BAD") is None


async def test_api_failure_does_not_propagate():
    cache = PriceCache()
    source = MassiveDataSource(api_key="k", price_cache=cache)
    source._client = MagicMock()
    source._tickers = ["AAPL"]
    source._client.get_snapshot_all.side_effect = RuntimeError("429 rate limit")

    await source._poll_once()          # swallowed and logged
    assert len(cache) == 0
```

Set `source._client` directly and drive `_poll_once()`; do not patch `asyncio.to_thread`. The `massive` package must be installed for `patch("app.market.massive_client.RESTClient")` to resolve — it is a core dependency, so `uv sync` is the fix if these fail with `AttributeError`.

### 15.4 Integration — the source contract

```python
async def test_source_seeds_cache_before_start_returns():
    cache = PriceCache()
    source = SimulatorDataSource(cache, update_interval=0.05)
    await source.start(["AAPL", "MSFT"])
    try:
        assert cache.get_price("AAPL") is not None     # no sleep needed
    finally:
        await source.stop()


async def test_stop_is_idempotent_and_halts_writes():
    cache = PriceCache()
    source = SimulatorDataSource(cache, update_interval=0.01)
    await source.start(["AAPL"])
    await asyncio.sleep(0.05)
    await source.stop()
    await source.stop()                                 # second call must not raise

    v = cache.version
    await asyncio.sleep(0.05)
    assert cache.version == v                           # nothing written after stop
```

### 15.5 SSE — the gap worth closing

`stream.py` is the least-covered module. One integration test using `httpx.ASGITransport` against a minimal app proves the wire format end to end:

```python
async def test_stream_emits_retry_then_price_payload():
    cache = PriceCache()
    cache.update("AAPL", 190.0)

    app = FastAPI()
    app.include_router(create_stream_router(cache))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream("GET", "/api/stream/prices") as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            chunks = []
            async for line in r.aiter_lines():
                chunks.append(line)
                if len(chunks) > 4:
                    break

    text = "\n".join(chunks)
    assert "retry: 1000" in text
    payload = json.loads(text.split("data: ", 1)[1].splitlines()[0])
    assert payload["AAPL"]["price"] == 190.0
    assert payload["AAPL"]["direction"] in {"up", "down", "flat"}
```

Because `create_stream_router()` registers `/prices` on a module-level router, calling it in several tests registers the route repeatedly (§17). Until that is fixed, call it once per test session or accept the duplicate registration — FastAPI serves the first match, so tests still pass.

### 15.6 E2E hooks

For Playwright (`test/`), the simulator is the right source: no key, no network, deterministic enough. Useful assertions: the watchlist shows 10 rows with non-empty prices within a few seconds of load; at least one cell gains and then loses the flash class; the connection dot is green; adding a ticker via the UI makes an 11th row appear with a price.

---

## 16. Error handling and edge cases

| Situation | Behaviour | Where |
|---|---|---|
| Empty ticker list at startup | `start([])` succeeds; simulator's `step()` returns `{}`; poller returns early. SSE sends nothing until a ticker is added | `simulator.py`, `massive_client.py` |
| Cache miss during a trade | `ensure_tracked` adds the ticker and waits up to 20 s, then 404s | §12.4 |
| Cache miss during valuation | Fall back to `avg_cost`, log a warning — never 0.0 | §12.5 |
| Invalid `MASSIVE_API_KEY` | Every poll logs `Massive poll failed: 401 ...`; app stays up with an empty cache. Check the factory's INFO line to confirm which source was selected | `massive_client.py` |
| Rate limit (429) | Poll logged and skipped; next interval retries. Raise `poll_interval` if persistent | `massive_client.py` |
| One ticker missing from a snapshot | Its cache entry goes stale (keeps the last price); others update | `massive_client.py` |
| Simulator step raises | Logged with traceback; that tick is skipped, loop continues | `simulator.py` |
| Client disconnects mid-stream | Detected within ~500 ms, generator exits, task freed | `stream.py` |
| Server shutdown with open streams | `CancelledError` caught and logged; `source.stop()` awaits loop termination | `stream.py`, both sources |
| Ticker added twice | No-op in both sources and in SQLite (`UNIQUE(user_id, ticker)`) | all |
| Price drifts to a pathological value | Cannot go ≤ 0 (GBM is multiplicative). Over a very long session drift dominates; restarting the container reseeds | `simulator.py` |
| Restart | Cache is in-memory: prices reset to seeds (simulator) or to live values (Massive). Positions, cash and trade history persist in SQLite | by design |

---

## 17. Hardening backlog

Known, low-severity divergences between the built code and the ideal. None blocks integration; each is a small, self-contained fix.

1. **`version` is read outside the lock.** Reading an `int` is atomic under the GIL, so this is safe on CPython today. On a free-threaded build (PEP 703) it becomes a genuine race. Fix: read it under `self._lock`.
2. **`ts = timestamp or time.time()` treats `0.0` as absent.** A Unix timestamp of exactly 0 is not a real market timestamp, so this has no practical impact; `timestamp if timestamp is not None else time.time()` is still the correct expression.
3. **`stream.py` holds a module-level `APIRouter`.** `create_stream_router()` registers `/prices` on that shared instance, so calling it twice registers the route twice. Fix: construct the `APIRouter` inside the factory.
4. **`start()` is not guarded against a second call.** The interface documents it as undefined behaviour; a `RuntimeError` on re-entry would be friendlier than silently orphaning the first background task.
5. **Poll and tick intervals are not configurable by env.** Fine today; if a paid-tier user wants 2 s polling, add it in `factory.py` (§10), not inside the sources.
6. **No SSE heartbeat.** When nothing changes for a long time (Massive out of hours), some proxies close an idle connection. `EventSource` reconnects automatically, so this is cosmetic, but emitting `: keepalive\n\n` every ~20 idle seconds would avoid the churn.

---

## 18. Quick reference

```python
from app.market import (
    PriceCache,                  # thread-safe price store
    PriceUpdate,                 # immutable price snapshot
    MarketDataSource,            # ABC: start/stop/add_ticker/remove_ticker/get_tickers
    create_market_data_source,   # factory (reads MASSIVE_API_KEY)
    create_stream_router,        # FastAPI router → GET /api/stream/prices
)

cache = PriceCache()
source = create_market_data_source(cache)
await source.start(["AAPL", "GOOGL", "MSFT"])

cache.get_price("AAPL")      # float | None  ← always check for None
cache.get("AAPL")            # PriceUpdate | None
cache.get_all()              # dict[str, PriceUpdate]
cache.version                # int, bumps on every write

await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")
source.get_tickers()         # list[str] (sync)
await source.stop()
```

| Knob | Default | Where |
|---|---|---|
| Simulator tick | 0.5 s | `SimulatorDataSource(update_interval=...)` |
| Shock probability | 0.001 per ticker per tick | `SimulatorDataSource(event_probability=...)` |
| Massive poll | 15.0 s | `MassiveDataSource(poll_interval=...)` |
| SSE cache poll | 0.5 s | `_generate_events(interval=...)` |
| SSE client retry | 1000 ms | `retry:` directive in `stream.py` |
| `dt` | `0.5 / (252·6.5·3600)` | `GBMSimulator.DEFAULT_DT` |

**Commands**

```bash
cd backend
uv sync --extra dev                        # install (massive is a core dep)
uv run --extra dev pytest -v               # 73 tests
uv run --extra dev pytest --cov=app        # coverage
uv run --extra dev ruff check app/ tests/  # lint
uv run market_data_demo.py                 # live terminal dashboard
```

---

## Appendix — relationship to other planning docs

| Document | Relationship |
|---|---|
| `PLAN.md` | Product and architecture spec. §6 (Market Data), §8 (endpoints), §10 (frontend) are the requirements this design implements; §13 raised the open questions answered in §13 here |
| `MARKET_DATA_SUMMARY.md` | Short status summary of what shipped — module list, test counts, review fixes |
| `archive/MARKET_DATA_DESIGN.md` | The pre-implementation design. Superseded by this document, which reflects the code as built |
| `archive/MARKET_INTERFACE.md` | Early interface sketch; `PriceUpdate` has since moved `change`/`direction` to computed properties and the cache gained a version counter |
| `archive/MARKET_SIMULATOR.md` | Early GBM sketch; the maths is unchanged, constants moved into `seed_prices.py` |
| `archive/MASSIVE_API.md` | Massive/Polygon.io API reference — endpoint shapes, response fields, rate limits. Still the reference for anyone extending the client |
| `archive/MARKET_DATA_REVIEW.md` | Code review that produced the seven fixes listed in the summary |
