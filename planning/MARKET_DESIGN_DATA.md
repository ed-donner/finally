# Market Data Backend — Detailed Design

**Status:** Implementation design, 2026-11-20. Owner: the market-data agent (`backend/app/market/`).
**Authority:** [PLAN.md](PLAN.md) §5–§8 and §12 describe the behaviour, and [API.md](API.md) is the wire contract. Where this document and those two disagree, they win and this document is fixed. Items here that would need a contract change are listed in §14 and are **not** implemented until `API.md` or `PLAN.md` changes first (AGENTS.md).
**Inputs:** [DATA_INTERFACE_DESIGN.md](DATA_INTERFACE_DESIGN.md), [SIMULATOR_RESEARCH.md](SIMULATOR_RESEARCH.md), [MASSIVE_API_RESEARCH.md](MASSIVE_API_RESEARCH.md), [REVIEW.md](REVIEW.md) (M2, M13, M15).

**How to use this document:** §4–§11 contain the complete source of every module in `backend/app/market/` plus the SSE route. §13 contains the complete test suite. The code was assembled into a package and run on Python 3.12: **48 tests pass**. An agent can copy the files as they are, then adjust them to the store and route modules that the backend agent builds.

---

## 1. Summary

- **One interface, two sources.** `SimulatorSource` and `MassiveSource` implement `PriceSource` (PLAN §6). `make_price_source()` is the only code that branches on `MASSIVE_API_KEY`.
- **Pull model with one driver.** `MarketService` runs one asyncio task. Every 500 ms it calls `source.tick()`, applies the updates to the `PriceCache`, and publishes one `prices` event. Every 5 s it publishes a `heartbeat`. The simulator does its maths inside `tick()`. The Massive client runs its own poll task and buffers results, so its `tick()` only drains the buffer. This fits PLAN's synchronous `tick()` to a network source and settles REVIEW M2 without changing the interface.
- **The cache is the only writer of per-ticker state.** It derives `previous_price`, `direction`, `change_pct`, the 600-point history, and the `stale` flag. Sources report observations and nothing else.
- **SSE fan-out serializes each event once.** Every client has a bounded queue. A client that falls behind is disconnected, and `EventSource` reconnects and gets a fresh `snapshot`.
- **Massive is budgeted.** Every request goes through one priority token bucket (poll > validate > baseline). Concurrent `add_ticker` calls are batched into one snapshot request. `prev_close` comes from `prevDay.c`, and `/prev` is called only as a fallback. After 3 consecutive failed polls the source reports `degraded`.

---

## 2. Requirements Traceability

| Requirement | Source | Where it is met |
|---|---|---|
| `PriceSource`: `start`, `stop`, `add_ticker`, `remove_ticker`, `tick` | PLAN §6 | §5 `source.py` |
| `UnknownTicker` on unpriceable symbols | PLAN §6, §8 | §4 `types.py`, §7 validation |
| Cache: `price`, `previous_price`, `timestamp`, `prev_close`, 600-point history | PLAN §6 | §8 `cache.py` |
| Cache covers watchlist ∪ held | PLAN §6 | §10 membership rules |
| GBM exact step, Δt = 30 trading s, one market factor, β | PLAN §6 | §6 `simulator.py` |
| Jumps: p = 1/1200, J ∈ [2%, 5%], random sign, multiplicative, injectable, `SIM_EVENTS_ENABLED` | PLAN §6 | §6 `JumpModel`, §11 factory |
| Unknown symbol: μ 5%, σ 25%, β 0.5, $100.00 | PLAN §6 | §6 `seed_prices.py` |
| Seeded RNG (`SIM_SEED`) | PLAN §6 | §6 |
| Massive: batched snapshot, chunk 250, token bucket, poll priority, interval stretch | PLAN §6 | §7 |
| 429/5xx keep last prices; degraded after 3 failures, ok after 1 success | PLAN §6 | §7 `poll_once` |
| Empty result, 404, or error row → not found | PLAN §6, MASSIVE_API_RESEARCH §5.3 | §7 `found_tickers` |
| SSE: `retry: 1000`, `snapshot` on connect, `prices` deltas with `id`, `heartbeat` every 5 s | API.md | §9, §10 |
| Unpriced ticker listed with nulls in `snapshot` | API.md | §8 `to_wire` |
| `price_state` restore; Massive `stale` values; snapshot task skips unpriced | PLAN §7 | §8 `restore`, §10 `valuation_price`, §11 |
| Fill at latest cached price, else `409 price_unavailable` | PLAN §8 | §10 `fill_price` |
| History is appended on every source update | REVIEW M13 | §8 `apply` |
| Single worker, in-process state | PLAN §3 | §3 |

---

## 3. Architecture

### 3.1 Module layout

```
backend/app/market/
├── __init__.py
├── types.py          # PriceUpdate, SourceHealth, StoredPrice, errors, ticker rule, time helpers
├── source.py         # PriceSource ABC
├── seed_prices.py    # Simulator seed table
├── simulator.py      # SimulatorSource, gbm_step, factor_shock, JumpModel
├── rate_limiter.py   # TokenBucketLimiter, Priority
├── massive.py        # MassiveSource, snapshot parsing, chunking, interval stretch
├── cache.py          # PriceCache, TickerState
├── broadcaster.py    # Broadcaster, Subscription, format_sse
├── service.py        # MarketService: driver loop, membership, prices for other modules
└── factory.py        # make_price_source(env)
backend/app/api/stream.py   # GET /api/stream/prices
```

Dependencies: `httpx` (Massive client) and `tzdata` (`python:3.12-slim` ships no zoneinfo database, and `massive.py` needs `America/New_York`). For tests: `pytest` and `pytest-asyncio` (`asyncio_mode = "auto"`). The simulator uses `random.Random` and `math` only, so it does not need numpy.

```toml
# backend/pyproject.toml (excerpt)
[project]
dependencies = ["fastapi", "uvicorn[standard]", "httpx", "tzdata", "python-dotenv"]

[dependency-groups]
dev = ["pytest", "pytest-asyncio"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

### 3.2 Data flow

```
                       ┌──────────────── MarketService (one asyncio task, every 500 ms) ────────────────┐
 SimulatorSource ──┐   │                                                                                 │
   (maths in tick) │   │  updates = source.tick()  ──►  changed = cache.apply(updates)                   │
                   ├──►│                                   │                                             │
 MassiveSource ────┘   │                                   └─► broadcaster.publish("prices", json)       │
   poll task ──► buffer│  every 5 s: broadcaster.publish("heartbeat", json(source.health))               │
   (tick drains it)    └─────────────────────────────────────────────────────────────────────────────────┘
                                         │                                   │
                       portfolio/watchlist services                  per-client asyncio.Queue
                       fill_price / valuation_price                           │
                       ensure / release / set_members              GET /api/stream/prices ──► EventSource
```

### 3.3 Concurrency model

Everything runs on the single uvicorn event loop (PLAN §3: one worker).

| Task | Created by | Cadence | Touches |
|---|---|---|---|
| `market-driver` | `MarketService.start()` | 500 ms tick, 5 s heartbeat | source `tick()`, cache, broadcaster |
| `massive-poll` | `MassiveSource.start()` | `current_interval()` (15 s by default) | limiter, HTTP, its own buffer |
| validation loop | first `add_ticker` after start | on demand | limiter, HTTP, futures |
| baseline fetch | `_schedule_baseline` | at most once per ticker per market day | limiter, HTTP, `_prev_close` |
| limiter grant loop | first `acquire()` | on demand | waiter heap |
| one generator per SSE client | `stream_prices` | event-driven | its own queue |

Nothing in the hot path blocks: `tick()`, `cache.apply()` and `publish()` are synchronous and CPU-cheap (a few dozen Gaussian draws and one `json.dumps` per tick). Because these steps never `await`, each one runs to completion before another task gets the loop. Taking a snapshot and subscribing a client therefore happen with no tick in between (§9.2).

---

## 4. Shared Types — `types.py`

```python
"""Shared value types for the market-data package. No I/O, no asyncio."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")

SourceName = Literal["simulator", "massive"]
SourceStatus = Literal["ok", "degraded"]
Direction = Literal["up", "down", "flat"]


class InvalidTicker(ValueError):
    """Ticker failed the format rule. Routes map this to 400 invalid_request."""


class UnknownTicker(Exception):
    """The source cannot price this ticker. Routes map this to 404 unknown_ticker."""

    def __init__(self, ticker: str) -> None:
        super().__init__(f"Unknown ticker: {ticker}")
        self.ticker = ticker


class SourceUnavailable(Exception):
    """The source could not answer (network/plan error), so existence is unknown."""


def normalize_ticker(raw: str) -> str:
    """Uppercase, strip, and check the format rule (PLAN §8, API.md conventions)."""
    ticker = raw.strip().upper()
    if not TICKER_RE.fullmatch(ticker):
        raise InvalidTicker(raw)
    return ticker


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """One observation from a source. The cache derives everything else."""

    ticker: str
    price: float                 # dollars, > 0
    timestamp_ms: int            # UTC epoch milliseconds
    prev_close: float | None = None  # None = "not known by this update"


@dataclass(frozen=True, slots=True)
class SourceHealth:
    status: SourceStatus = "ok"
    consecutive_failures: int = 0


@dataclass(frozen=True, slots=True)
class StoredPrice:
    """One row of the price_state table (PLAN §7)."""

    ticker: str
    price: float
    prev_close: float | None
    updated_at_ms: int


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def iso_ms(ts_ms: int) -> str:
    """Epoch ms -> '2026-11-18T14:30:00.500Z' (API.md timestamp format)."""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_s(ts_ms: int) -> str:
    """Epoch ms -> '2026-11-18T14:30:05Z' (heartbeat ts)."""
    dt = datetime.fromtimestamp(ts_ms // 1000, tz=UTC)
    return dt.isoformat().replace("+00:00", "Z")
```

Notes:

- `PriceUpdate` is an **observation**. `prev_close=None` means "this update does not say". It does not mean "the baseline is unknown". The cache never lets `None` erase a known baseline (§8).
- `SourceUnavailable` is used when existence cannot be checked, for example when Massive returns 503 during validation. Its HTTP mapping is open item §14 #1.
- `InvalidTicker` is raised by `normalize_ticker`. Routes map it to `400 invalid_request` (API.md). Sources receive only normalized symbols.

---

## 5. The Interface — `source.py`

```python
"""The one seam: everything downstream depends only on PriceSource (PLAN §6)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from .types import PriceUpdate, SourceHealth, SourceName


class PriceSource(ABC):
    name: ClassVar[SourceName]

    @abstractmethod
    async def start(self) -> None:
        """Begin producing prices. add_ticker() calls made *before* start() register
        tickers restored from the database and are not validated (they were
        validated when first added)."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop background work and release network resources. Idempotent."""

    @abstractmethod
    async def add_ticker(self, symbol: str) -> None:
        """Start pricing `symbol` (already normalized). Idempotent.
        Raises UnknownTicker if the source cannot price it, or SourceUnavailable
        if the source could not be asked."""

    @abstractmethod
    async def remove_ticker(self, symbol: str) -> None:
        """Stop pricing `symbol`. Idempotent; unknown symbols are ignored."""

    @abstractmethod
    def tick(self) -> list[PriceUpdate]:
        """Return the updates produced since the previous call. Must not block or
        do I/O. Called by MarketService every TICK_INTERVAL_S (500 ms)."""

    @property
    def health(self) -> SourceHealth:
        """Upstream health for the heartbeat. Sources without an upstream are always ok."""
        return SourceHealth()
```

Design decisions:

1. **`tick()` stays synchronous (PLAN §6).** It must not perform I/O. A network source does its I/O in its own task and buffers the results. `MarketService` is the only caller and the only clock, so downstream code never sees source timing. This is REVIEW M2's second option, applied without changing the interface.
2. **Adds before `start()` are trusted.** At startup the service registers the persisted watchlist and held tickers before it starts the source. The Massive client skips validation for these. Otherwise a restart with 10 tickers would spend 10 validation calls of a 5-per-minute budget. Every ticker in the database was validated when it was first added.
3. **`health` is a concrete property with an `ok` default.** It is not abstract, so the simulator needs no code for it, and the core contract stays the five PLAN methods (DATA_INTERFACE_DESIGN D3).
4. **Idempotence.** Adding a symbol that is already active and removing one that is not active are both no-ops. The shared contract test (§13.6) checks this for both sources.

---

## 6. Simulator — `seed_prices.py`, `simulator.py`

### 6.1 The maths (from SIMULATOR_RESEARCH.md, rechecked)

Per tick, for each active ticker *i*:

```
z_m ~ N(0,1)                                   one market shock per tick
ε_i ~ N(0,1)                                   independent per ticker
z_i = β_i·z_m + √(1−β_i²)·ε_i                  Var(z_i)=1, Corr(z_i,z_j)=β_i·β_j
S_i ← S_i · exp((μ_i − σ_i²/2)·Δt + σ_i·√Δt·z_i)      exact GBM step
S_i ← S_i · (1 ± J)   with prob 1/1200, J ~ U[0.02, 0.05]   (jumps on)
```

| Constant | Value | Meaning |
|---|---|---|
| `TRADING_SECONDS_PER_YEAR` | 252 × 6.5 × 3600 = 5,896,800 | |
| `SIM_TICK_MARKET_SECONDS` | 30 | market time per tick (PLAN §6) |
| `DT_YEARS` | 5.088 × 10⁻⁶ | Δt per tick |
| One-tick sd at σ = 25% | 0.056% | "about 0.06%" (PLAN §6) |
| Jump variance / diffusion variance at σ = 25% | ≈ 3.4× | realized vol ≈ 52% with jumps on (SIMULATOR_RESEARCH §2.4) |
| Speed | 1 real minute ≈ 1 market hour | 1 real hour ≈ 9 trading days |

### 6.2 Seed table

```python
"""Simulator seed table (PLAN §6). Values are illustrative product inputs, not quotes."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SeedParams:
    start: float   # starting price, dollars
    mu: float      # annual drift
    sigma: float   # annual volatility
    beta: float    # loading on the single market factor, 0 <= beta < 1


SEEDS: dict[str, SeedParams] = {
    #               start    mu     sigma  beta
    "AAPL":  SeedParams(230.00, 0.08, 0.25, 0.7),
    "GOOGL": SeedParams(175.00, 0.08, 0.28, 0.7),
    "MSFT":  SeedParams(420.00, 0.08, 0.25, 0.7),
    "AMZN":  SeedParams(200.00, 0.08, 0.30, 0.7),
    "TSLA":  SeedParams(250.00, 0.05, 0.55, 0.7),
    "NVDA":  SeedParams(130.00, 0.10, 0.45, 0.7),
    "META":  SeedParams(560.00, 0.08, 0.35, 0.7),
    "JPM":   SeedParams(220.00, 0.07, 0.22, 0.6),
    "V":     SeedParams(290.00, 0.07, 0.22, 0.6),
    "NFLX":  SeedParams(700.00, 0.08, 0.38, 0.7),
}

# Any well-formed symbol not in SEEDS (PLAN §6 "Unknown tickers").
DEFAULT_PARAMS = SeedParams(start=100.00, mu=0.05, sigma=0.25, beta=0.5)


def params_for(ticker: str) -> SeedParams:
    return SEEDS.get(ticker, DEFAULT_PARAMS)
```

The starting prices are **illustrative placeholders**. They are not market quotes, and the product owner may change them (SIMULATOR_RESEARCH §7, open question 4). σ and β follow the research proposal: β is 0.7 for tech and 0.6 for financials.

### 6.3 Implementation

```python
"""GBM simulator with one market factor and random jumps (PLAN §6, SIMULATOR_RESEARCH.md)."""
from __future__ import annotations

import bisect
import math
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .seed_prices import DEFAULT_PARAMS, SEEDS, SeedParams
from .source import PriceSource
from .types import PriceUpdate, now_ms

TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600          # 5,896,800
SIM_TICK_MARKET_SECONDS = 30.0                       # market time per tick
DT_YEARS = SIM_TICK_MARKET_SECONDS / TRADING_SECONDS_PER_YEAR  # ~5.088e-6
SIM_SEED = 20261118


def gbm_step(price: float, mu: float, sigma: float, dt: float, z: float) -> float:
    """Exact GBM discretization: always positive, Ito term exact at any dt."""
    return price * math.exp((mu - 0.5 * sigma * sigma) * dt + sigma * math.sqrt(dt) * z)


def factor_shock(beta: float, z_market: float, eps: float) -> float:
    """z_i = beta*z_m + sqrt(1-beta^2)*eps  =>  Var(z_i)=1, Corr(z_i,z_j)=beta_i*beta_j."""
    return beta * z_market + math.sqrt(1.0 - beta * beta) * eps


@dataclass(frozen=True, slots=True)
class JumpModel:
    """Multiplicative Bernoulli jump, applied after diffusion (PLAN §6 'Random events')."""

    prob: float = 1 / 1200
    min_size: float = 0.02
    max_size: float = 0.05

    def draw(self, rng: random.Random) -> float:
        """Return the price multiplier for one ticker-tick: 1.0, or 1 +/- J."""
        if rng.random() >= self.prob:
            return 1.0
        size = rng.uniform(self.min_size, self.max_size)
        sign = 1.0 if rng.random() < 0.5 else -1.0
        return 1.0 + sign * size


class SimulatorSource(PriceSource):
    name = "simulator"

    def __init__(
        self,
        *,
        rng_seed: int = SIM_SEED,
        params: Mapping[str, SeedParams] | None = None,
        dt_years: float = DT_YEARS,
        jumps: JumpModel | None = JumpModel(),
        restored: Mapping[str, tuple[float, float | None]] | None = None,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self._rng = random.Random(rng_seed)          # one RNG, owned here
        self._params = dict(SEEDS if params is None else params)
        self._dt = dt_years
        self._jumps = jumps                           # None == SIM_EVENTS_ENABLED=false
        self._restored = dict(restored or {})         # ticker -> (price, prev_close)
        self._clock = clock
        self._price: dict[str, float] = {}           # full precision; kept after remove
        self._prev_close: dict[str, float] = {}
        self._active: list[str] = []                 # sorted => fixed draw order

    async def start(self) -> None:   # driven by MarketService calling tick()
        pass

    async def stop(self) -> None:
        pass

    async def add_ticker(self, symbol: str) -> None:
        if symbol in self._active:
            return
        if symbol not in self._price:
            stored = self._restored.pop(symbol, None)
            if stored is not None:
                price, prev_close = stored
                self._price[symbol] = price
                self._prev_close[symbol] = prev_close if prev_close else price
            else:
                start = self._param(symbol).start
                self._price[symbol] = start
                self._prev_close[symbol] = start
        bisect.insort(self._active, symbol)

    async def remove_ticker(self, symbol: str) -> None:
        if symbol in self._active:
            self._active.remove(symbol)   # price kept: re-adding resumes, no reset

    def tick(self) -> list[PriceUpdate]:
        if not self._active:
            return []
        rng = self._rng
        # Draw order is part of the reproducibility contract:
        # 1) market shock, 2) one eps per ticker in sorted order, 3) jump draws in sorted order.
        z_m = rng.gauss(0.0, 1.0)
        eps = [rng.gauss(0.0, 1.0) for _ in self._active]
        for ticker, e in zip(self._active, eps):
            p = self._param(ticker)
            z = factor_shock(p.beta, z_m, e)
            self._price[ticker] = gbm_step(self._price[ticker], p.mu, p.sigma, self._dt, z)
        if self._jumps is not None:
            for ticker in self._active:
                self._price[ticker] *= self._jumps.draw(rng)
        ts = self._clock()
        return [
            PriceUpdate(
                ticker=t,
                price=max(0.01, round(self._price[t], 2)),
                timestamp_ms=ts,
                prev_close=round(self._prev_close[t], 2),
            )
            for t in self._active
        ]

    # --- test helpers -------------------------------------------------------
    def raw_price(self, ticker: str) -> float:
        """Unrounded internal price. Statistical tests use this, never the rounded one."""
        return self._price[ticker]

    def _param(self, ticker: str) -> SeedParams:
        return self._params.get(ticker, DEFAULT_PARAMS)
```

Implementation rules, each covered by a test in §13.1:

- **Draw order is part of the contract:** first `z_m`, then one `ε` per active ticker in sorted order, then the jump draws per ticker in sorted order. Changing it changes every price for a given seed, so the reproducibility test changes in the same commit.
- **Full precision inside, cents outside.** `_price` is never rounded, so rounding cannot distort the walk. The emitted `PriceUpdate.price` is rounded to cents. A sub-half-cent move therefore emits an unchanged price, which the cache treats as "not changed" for the `prices` event (§8).
- **Time never comes from the wall clock.** Every tick advances market time by exactly `dt_years`, however late the tick ran. The wall clock is used only for `timestamp_ms`.
- **Restart (PLAN §7):** a ticker with a `price_state` row resumes from its stored `price`, and its `prev_close` comes from the stored `prev_close`. The day change therefore survives a restart, which settles SIMULATOR_RESEARCH §4. A ticker without a row starts from the seed.
- **Remove, then add again** resumes from the last simulated price instead of resetting to the seed. This avoids a visible jump when a held ticker leaves the watchlist and comes back.
- **`jumps=None`** is how `SIM_EVENTS_ENABLED=false` is implemented. `JumpModel(prob=...)` is the injectable `jump_prob`.

---

## 7. Massive Client — `rate_limiter.py`, `massive.py`

### 7.1 Endpoints and field mapping

| Use | Request | Priority |
|---|---|---|
| Price poll | `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=A,B,…` (≤ 250 per request) | `POLL` (0) |
| `add_ticker` validation | same endpoint, batched symbols | `VALIDATE` (1) |
| Baseline fallback | `GET /v2/aggs/ticker/{T}/prev?adjusted=true` | `BASELINE` (2) |

| `PriceUpdate` field | Massive field | Rule |
|---|---|---|
| `price` | `lastTrade.p` → `min.c` → `day.c` | first positive value. If none, the row is skipped and the last price is kept (this covers the 3:30–4:00 AM ET reset window) |
| `timestamp_ms` | `lastTrade.t` → `updated` | unit chosen by magnitude (s/ms/µs/ns, S1 open). Falls back to `now` if missing or more than one day in the future |
| `prev_close` | `prevDay.c` | positive, otherwise `None` (never 0). If missing and not yet known, one `/prev` call per ticker per day |

Massive's `todaysChangePerc` is ignored. `change_pct` is always computed by the cache from `prev_close`, so both sources use the same definition (MASSIVE_API_RESEARCH §5.1).

Example snapshot row and the update it produces (from `test_parse_prefers_last_trade_and_converts_ns`):

```json
{"ticker": "AAPL", "day": {"c": 191.0}, "prevDay": {"c": 190.10},
 "lastTrade": {"p": 191.42, "t": 1763476199000000000}, "updated": 1763476199500000000}
```
```python
PriceUpdate(ticker="AAPL", price=191.42, timestamp_ms=1763476199000, prev_close=190.10)
```

### 7.2 Call budget

`MASSIVE_CALLS_PER_MINUTE` (default 5) sizes one token bucket, with a burst equal to the per-minute rate. Polls may use at most `calls_per_minute − 1` calls per minute, which keeps one call per minute spare for validation and baselines:

```
calls_per_poll   = ceil(n_tickers / 250)
interval         = max(MASSIVE_POLL_INTERVAL_SECONDS, 60 · calls_per_poll / (calls_per_minute − 1))
```

| Tickers | Calls/min | Configured | Effective interval | Polls/min |
|---|---|---|---|---|
| 10 | 5 | 15 s | 15 s | 4 (+1 spare) |
| 600 | 5 | 15 s | 45 s | 1.33 × 3 chunks = 4 |
| 10 | 2 | 15 s | 60 s | 1 |
| 10 | 60 | 15 s | 15 s | 4 |

When the interval is stretched, a warning is logged once (PLAN §6).

**Validation batching.** An LLM turn can add up to 10 tickers. Validating them one by one at 1 spare call per minute would take 10 minutes. Instead, every `add_ticker` call that arrives while a validation is waiting for a token joins the same batch, and one snapshot request answers them all (`test_concurrent_adds_share_one_request`). Found symbols are also put into the buffer, so their first price shows up on the next tick instead of after the next poll.

### 7.3 Rate limiter

```python
"""Token bucket with priorities for every Massive request (PLAN §6 'Rate limiter')."""
from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from collections.abc import Callable
from enum import IntEnum


class Priority(IntEnum):
    POLL = 0        # price polls first (PLAN §6)
    VALIDATE = 1    # add_ticker validation (user is waiting on an HTTP response)
    BASELINE = 2    # /prev fallback, lowest


class TokenBucketLimiter:
    """`calls_per_minute` tokens, refilled continuously; burst = calls_per_minute.
    Waiters are served strictly by (priority, arrival order)."""

    def __init__(self, calls_per_minute: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._capacity = float(max(1, calls_per_minute))
        self._rate = self._capacity / 60.0          # tokens per second
        self._tokens = self._capacity
        self._clock = clock
        self._updated = clock()
        self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def acquire(self, priority: Priority) -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (int(priority), next(self._seq), fut))
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._grant_loop())
        self._wake.set()
        await fut     # cancellation leaves a done future that the loop skips

    async def aclose(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
        self._updated = now

    async def _grant_loop(self) -> None:
        while True:
            while self._waiters and self._waiters[0][2].done():
                heapq.heappop(self._waiters)          # cancelled waiter
            if not self._waiters:
                self._wake.clear()
                await self._wake.wait()
                continue
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                _, _, fut = heapq.heappop(self._waiters)
                fut.set_result(None)
                continue
            delay = (1.0 - self._tokens) / self._rate
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass
```

The limiter serves waiters strictly by `(priority, arrival)`. A cancelled waiter (for example, a client that disconnects during validation) leaves a finished future that the grant loop drops, and it uses no token. Baseline calls can be starved by a steady stream of higher-priority work. This is acceptable because they are the lowest priority by design.

### 7.4 Client

```python
"""Massive (formerly Polygon.io) REST polling client (PLAN §6, MASSIVE_API_RESEARCH.md)."""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .rate_limiter import Priority, TokenBucketLimiter
from .source import PriceSource
from .types import PriceUpdate, SourceHealth, SourceUnavailable, UnknownTicker, now_ms

log = logging.getLogger(__name__)

# Pinned constants (PLAN §5: not env vars). S1(e): confirm host with one live call;
# the legacy host is https://api.polygon.io.
MASSIVE_BASE_URL = "https://api.massive.com"
SNAPSHOT_PATH = "/v2/snapshot/locale/us/markets/stocks/tickers"
PREV_PATH = "/v2/aggs/ticker/{ticker}/prev"
SNAPSHOT_CHUNK = 250          # S1(b): chunk at min(250, confirmed limit)
DEGRADED_AFTER = 3            # consecutive failed polls -> source_status "degraded"
REQUEST_TIMEOUT_S = 10.0
MARKET_TZ = ZoneInfo("America/New_York")   # needs the `tzdata` package in slim images


class MassiveError(Exception):
    """A request failed. `transient` is False for plan/auth problems (403)."""

    def __init__(self, message: str, *, status: int | None = None, transient: bool = True) -> None:
        super().__init__(message)
        self.status = status
        self.transient = transient


# --------------------------------------------------------------------------- parsing
def to_epoch_ms(value: float | int) -> int:
    """Massive timestamps are ns in practice; accept s/ms/us/ns by magnitude (S1: confirm)."""
    v = float(value)
    if v >= 1e17:
        return int(v // 1_000_000)      # ns
    if v >= 1e14:
        return int(v // 1_000)          # us
    if v >= 1e11:
        return int(v)                   # ms
    return int(v * 1000)                # s


def _dig(row: dict, *path: str) -> Any:
    cur: Any = row
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _positive(*values: Any) -> float | None:
    for v in values:
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0:
            return float(v)
    return None


def parse_snapshot_row(row: Any, now: int) -> PriceUpdate | None:
    """One `tickers[]` element -> PriceUpdate, or None if it carries no usable price."""
    if not isinstance(row, dict) or row.get("error"):
        return None
    ticker = row.get("ticker")
    if not isinstance(ticker, str):
        return None
    price = _positive(_dig(row, "lastTrade", "p"), _dig(row, "min", "c"), _dig(row, "day", "c"))
    if price is None:
        return None                      # e.g. 3:30-4:00 AM ET reset window: keep last price
    raw_ts = _positive(_dig(row, "lastTrade", "t"), row.get("updated"))
    ts = to_epoch_ms(raw_ts) if raw_ts else now
    if ts > now + 86_400_000:            # unit guess wrong or clock skew: trust our clock
        ts = now
    prev_close = _positive(_dig(row, "prevDay", "c"))   # 0/missing -> None, never 0
    return PriceUpdate(ticker=ticker, price=price, timestamp_ms=ts, prev_close=prev_close)


def found_tickers(rows: Iterable[Any]) -> set[str]:
    """Tickers present without an `error` field. 404, empty list and error rows = not found."""
    return {
        r["ticker"] for r in rows
        if isinstance(r, dict) and isinstance(r.get("ticker"), str) and not r.get("error")
    }


def chunked(items: list[str], size: int = SNAPSHOT_CHUNK) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def effective_poll_interval(configured_s: float, n_tickers: int, calls_per_minute: int) -> float:
    """Stretch the interval so polls fit the budget, keeping 1 call/min spare for
    validation and baselines when the budget allows it (PLAN §6 'Interval')."""
    calls_per_poll = max(1, math.ceil(n_tickers / SNAPSHOT_CHUNK))
    poll_budget = calls_per_minute - 1 if calls_per_minute > 1 else 1
    return max(configured_s, 60.0 * calls_per_poll / poll_budget)


# --------------------------------------------------------------------------- source
class MassiveSource(PriceSource):
    name = "massive"

    def __init__(
        self,
        api_key: str,
        *,
        poll_interval_s: float = 15.0,
        calls_per_minute: int = 5,
        base_url: str = MASSIVE_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,   # tests: httpx.MockTransport
        limiter: TokenBucketLimiter | None = None,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            # S1: confirm Bearer auth. If only ?apiKey= works, add it in an event hook
            # and redact it from every logged URL.
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=REQUEST_TIMEOUT_S,
            transport=transport,
        )
        self._poll_interval = poll_interval_s
        self._cpm = calls_per_minute
        self._limiter = limiter or TokenBucketLimiter(calls_per_minute)
        self._clock = clock
        self._tickers: set[str] = set()
        self._buffer: list[PriceUpdate] = []
        self._prev_close: dict[str, float] = {}
        self._baseline_day: dict[str, date] = {}
        self._failures = 0
        self._running = False
        self._stretch_warned = False
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._validator: asyncio.Task | None = None
        self._poller: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()

    # --- PriceSource ------------------------------------------------------------
    @property
    def health(self) -> SourceHealth:
        status = "degraded" if self._failures >= DEGRADED_AFTER else "ok"
        return SourceHealth(status=status, consecutive_failures=self._failures)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._poller = asyncio.create_task(self._poll_loop(), name="massive-poll")

    async def stop(self) -> None:
        self._running = False
        tasks = [t for t in (self._poller, self._validator, *self._background) if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._limiter.aclose()
        await self._client.aclose()

    async def add_ticker(self, symbol: str) -> None:
        if symbol in self._tickers:
            return
        if not self._running:                # bootstrap from the DB: trusted, no call
            self._tickers.add(symbol)
            return
        if not await self._validate(symbol):
            raise UnknownTicker(symbol)
        self._tickers.add(symbol)

    async def remove_ticker(self, symbol: str) -> None:
        self._tickers.discard(symbol)        # buffered updates are dropped by the cache

    def tick(self) -> list[PriceUpdate]:
        out, self._buffer = self._buffer, []
        return out

    # --- polling ------------------------------------------------------------------
    def current_interval(self) -> float:
        interval = effective_poll_interval(self._poll_interval, len(self._tickers), self._cpm)
        if interval > self._poll_interval and not self._stretch_warned:
            log.warning("Massive poll interval stretched to %.1fs to fit %d calls/min",
                        interval, self._cpm)
            self._stretch_warned = True
        return interval

    async def _poll_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                await self.poll_once()
            except Exception:                 # never let the poller die
                log.exception("Massive poll crashed")
                self._failures += 1
            await asyncio.sleep(max(0.0, self.current_interval() - (loop.time() - started)))

    async def poll_once(self) -> None:
        tickers = sorted(self._tickers)
        if not tickers:
            return
        ok = True
        for chunk in chunked(tickers):
            await self._limiter.acquire(Priority.POLL)
            try:
                rows = await self._snapshot(chunk)
            except MassiveError as e:
                ok = False
                level = logging.WARNING if e.transient else logging.ERROR
                log.log(level, "Massive poll failed (%s): %s", e.status, e)
                continue                      # keep last prices (PLAN §6 retry rule)
            self._ingest(rows, members_only=True)
        if ok:
            if self._failures >= DEGRADED_AFTER:
                log.info("Massive recovered after %d failed polls", self._failures)
            self._failures = 0
        else:
            self._failures += 1

    def _ingest(self, rows: list[Any], *, members_only: bool) -> None:
        now = self._clock()
        for row in rows:
            update = parse_snapshot_row(row, now)
            if update is None:
                continue
            t = update.ticker
            if members_only and t not in self._tickers:
                continue
            if update.prev_close is not None:
                self._prev_close[t] = update.prev_close
            elif t not in self._prev_close:
                self._schedule_baseline(t)
            self._buffer.append(
                PriceUpdate(t, update.price, update.timestamp_ms, self._prev_close.get(t))
            )

    # --- validation (batched: N concurrent add_ticker calls cost one request) ---------
    async def _validate(self, symbol: str) -> bool:
        fut = self._pending.get(symbol)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self._pending[symbol] = fut
            if self._validator is None or self._validator.done():
                self._validator = asyncio.create_task(self._validation_loop())
        return await asyncio.shield(fut)

    async def _validation_loop(self) -> None:
        while self._pending:
            await self._limiter.acquire(Priority.VALIDATE)
            batch, self._pending = self._pending, {}      # everything queued while waiting
            symbols = sorted(batch)
            try:
                rows: list[Any] = []
                for i, chunk in enumerate(chunked(symbols)):
                    if i:
                        await self._limiter.acquire(Priority.VALIDATE)
                    rows += await self._snapshot(chunk)
            except MassiveError as e:
                for fut in batch.values():
                    if not fut.done():
                        fut.set_exception(SourceUnavailable(f"Massive validation failed: {e}"))
                continue
            found = found_tickers(rows)
            self._ingest([r for r in rows if isinstance(r, dict) and r.get("ticker") in found],
                         members_only=False)      # first price shows up on the next tick
            for sym, fut in batch.items():
                if not fut.done():
                    fut.set_result(sym in found)

    # --- baseline fallback (only when prevDay.c is missing) ------------------------
    def _schedule_baseline(self, ticker: str) -> None:
        today = datetime.now(MARKET_TZ).date()
        if self._baseline_day.get(ticker) == today:
            return                                   # once per ticker per market day
        self._baseline_day[ticker] = today
        task = asyncio.create_task(self._fetch_baseline(ticker))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _fetch_baseline(self, ticker: str) -> None:
        await self._limiter.acquire(Priority.BASELINE)
        try:
            payload = await self._get(PREV_PATH.format(ticker=ticker), {"adjusted": "true"})
        except MassiveError as e:
            log.warning("Massive /prev failed for %s: %s", ticker, e)
            return
        results = (payload or {}).get("results") or []
        close = _positive(_dig(results[0], "c")) if results else None
        if close is not None:
            self._prev_close[ticker] = close     # attached to the ticker's next update

    # --- HTTP ---------------------------------------------------------------------
    async def _snapshot(self, symbols: list[str]) -> list[Any]:
        payload = await self._get(SNAPSHOT_PATH, {"tickers": ",".join(symbols)})
        rows = (payload or {}).get("tickers")
        return rows if isinstance(rows, list) else []

    async def _get(self, path: str, params: dict[str, str]) -> dict | None:
        """One request. Caller must already hold a limiter token. 404 -> None."""
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise MassiveError(f"{type(e).__name__} on {path}") from None  # no URL, no key
        if resp.status_code == 404:
            return None
        if resp.status_code in (401, 403):
            raise MassiveError("not authorized: check MASSIVE_API_KEY and plan tier",
                               status=resp.status_code, transient=False)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise MassiveError(f"HTTP {resp.status_code}", status=resp.status_code)
        if resp.status_code != 200:
            raise MassiveError(f"HTTP {resp.status_code}", status=resp.status_code)
        try:
            body = resp.json()
        except ValueError:
            raise MassiveError("invalid JSON", status=200) from None
        return body if isinstance(body, dict) else None
```

### 7.5 Failure handling

| Condition | Poll | Validation (`add_ticker`) | Baseline |
|---|---|---|---|
| 200 with rows | buffer updates, `failures = 0` | found → added; missing or `error` row → `UnknownTicker` | store `results[0].c` |
| 200 with no rows / 404 | success with nothing buffered | `UnknownTicker` | ignored |
| 429, 5xx, timeout, connection error, invalid JSON | keep last prices, `failures += 1`, warning | `SourceUnavailable` | warning, retried next day |
| 401 / 403 | as above, logged at **error** ("check key and plan tier") | `SourceUnavailable` | warning |

- A poll with several chunks counts as **one** failure if any chunk fails. The other chunks still update.
- `health` is `degraded` while `consecutive_failures ≥ 3`. One fully successful poll resets the count (PLAN §6, test `test_transient_errors_keep_prices_and_degrade_after_3`).
- Error messages never include the URL. The key travels in the `Authorization` header, so it cannot leak into logs through a URL. If spike S1 shows that only `?apiKey=` works, add it in an `httpx` request hook and keep URLs out of log lines.

### 7.6 What still depends on spike S1

| Constant / behaviour | Current value | Spike item |
|---|---|---|
| `MASSIVE_BASE_URL` | `https://api.massive.com` (legacy: `https://api.polygon.io`) | S1(e) |
| `Authorization: Bearer` | assumed | S1 (auth) |
| `SNAPSHOT_CHUNK` | 250 | S1(b) |
| `to_epoch_ms` magnitude rule | handles s/ms/µs/ns | S1 (timestamp unit) |
| `lastTrade` vs `min`/`day` fallback | handles both | S1 (Starter/Developer fields) |
| `/prev` not-found | 404 or empty `results` both → no baseline | S1(d) |

None of these changes the interface or the tests' structure. Only constants and the fixtures in §13.4 change.

---

## 8. Price Cache — `cache.py`

### 8.1 Rules

| Rule | Behaviour |
|---|---|
| Membership | Only members hold state. Updates for non-members are dropped, which covers a ticker removed between `tick()` and `apply()`. |
| `previous_price` | The price before this update. For the first update it equals `price`, so `direction` is `flat`. |
| `direction` | `up` / `down` / `flat` from `price` vs `previous_price`. `null` when unpriced. |
| `change_pct` | `round((price − prev_close) / prev_close × 100, 2)`, or `null` (API.md). |
| `prev_close` | Set when an update carries one. `None` never erases it. |
| History | `(timestamp_ms, price)` is appended on **every** accepted update, including unchanged prices. The window is 5 min in simulator mode and 2.5 h at a 15 s Massive poll (REVIEW M13). |
| Duplicate observation | Same timestamp, price and baseline as the current state is a no-op. This stops a Massive re-poll of an unchanged `lastTrade` from padding the history. |
| "Changed" (goes into `prices`) | first live price, a different price, or a different `prev_close`. |
| `stale` | Set by `restore()` from `price_state`. Cleared by the first live update, which always counts as changed. |

### 8.2 Implementation

```python
"""In-memory price cache: the only writer of per-ticker state (PLAN §6)."""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .types import Direction, PriceUpdate, StoredPrice, iso_ms

HISTORY_LEN = 600


@dataclass(slots=True)
class TickerState:
    ticker: str
    price: float | None = None
    previous_price: float | None = None
    prev_close: float | None = None
    timestamp_ms: int | None = None
    stale: bool = False   # True while the price comes from price_state, not a live update
    history: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=HISTORY_LEN))

    @property
    def direction(self) -> Direction | None:
        if self.price is None or self.previous_price is None:
            return None
        if self.price > self.previous_price:
            return "up"
        if self.price < self.previous_price:
            return "down"
        return "flat"

    @property
    def change_pct(self) -> float | None:
        if self.price is None or not self.prev_close:
            return None
        return round((self.price - self.prev_close) / self.prev_close * 100, 2)

    def to_wire(self, *, with_history: bool) -> dict:
        """Shape of one ticker in API.md `snapshot` (with history) / `prices` (with ticker)."""
        body: dict = {
            "price": self.price,
            "previous_price": self.previous_price,
            "prev_close": self.prev_close,
            "change_pct": self.change_pct,
            "direction": self.direction,
            "timestamp": iso_ms(self.timestamp_ms) if self.timestamp_ms is not None else None,
        }
        if with_history:
            body["history"] = [[ts, px] for ts, px in self.history]
        else:
            body = {"ticker": self.ticker, **body}
        return body


class PriceCache:
    def __init__(self) -> None:
        self._states: dict[str, TickerState] = {}

    # --- membership (watchlist ∪ held), managed by MarketService -------------
    @property
    def members(self) -> frozenset[str]:
        return frozenset(self._states)

    def add_member(self, ticker: str) -> None:
        self._states.setdefault(ticker, TickerState(ticker))

    def remove_member(self, ticker: str) -> None:
        self._states.pop(ticker, None)

    def get(self, ticker: str) -> TickerState | None:
        return self._states.get(ticker)

    # --- restart (PLAN §7 Restart Rules) --------------------------------------
    def restore(self, rows: Mapping[str, StoredPrice]) -> None:
        """Seed members from price_state. Values are marked stale; history stays empty."""
        for ticker, row in rows.items():
            state = self._states.get(ticker)
            if state is None or state.price is not None:
                continue
            state.price = row.price
            state.previous_price = row.price
            state.prev_close = row.prev_close
            state.timestamp_ms = row.updated_at_ms
            state.stale = True

    # --- the hot path ----------------------------------------------------------
    def apply(self, updates: Iterable[PriceUpdate]) -> list[TickerState]:
        """Apply source updates. Returns the states that changed, for the `prices` event."""
        changed: list[TickerState] = []
        for u in updates:
            state = self._states.get(u.ticker)
            if state is None:            # removed between source tick and apply: ignore
                continue
            if (
                not state.stale
                and state.timestamp_ms == u.timestamp_ms
                and state.price == u.price
                and (u.prev_close is None or u.prev_close == state.prev_close)
            ):
                continue                 # same observation again (e.g. Massive re-poll): no-op
            first_live = state.price is None or state.stale
            old_price, old_close = state.price, state.prev_close
            state.previous_price = old_price if old_price is not None else u.price
            state.price = u.price
            state.timestamp_ms = u.timestamp_ms
            if u.prev_close is not None:             # None never erases a known baseline
                state.prev_close = u.prev_close
            state.stale = False
            state.history.append((u.timestamp_ms, u.price))
            if first_live or u.price != old_price or state.prev_close != old_close:
                changed.append(state)
        return changed

    # --- readers ----------------------------------------------------------------
    def snapshot_tickers(self) -> dict[str, dict]:
        return {t: s.to_wire(with_history=True) for t, s in sorted(self._states.items())}

    def export_price_state(self) -> list[StoredPrice]:
        return [
            StoredPrice(t, s.price, s.prev_close, s.timestamp_ms or 0)
            for t, s in sorted(self._states.items())
            if s.price is not None
        ]
```

### 8.3 Worked example

| Step | Input | `price` | `previous_price` | `direction` | In `prices`? | History length |
|---|---|---|---|---|---|---|
| `add_member("X")` | | `null` | `null` | `null` | | 0 |
| apply | `X 10.00 @t1 pc 9.50` | 10.00 | 10.00 | flat | yes (first) | 1 |
| apply | `X 10.05 @t2` | 10.05 | 10.00 | up | yes | 2 |
| apply | `X 10.05 @t3` | 10.05 | 10.05 | flat | no | 3 |
| apply | `X 10.05 @t3` (re-poll) | 10.05 | 10.05 | flat | no | 3 |
| apply | `X 9.90 @t4 pc 10.05` (new day) | 9.90 | 10.05 | down | yes | 4 |

---

## 9. SSE Fan-out — `broadcaster.py`, `api/stream.py`

### 9.1 Broadcaster

```python
"""Fan-out of serialized SSE events to connected clients (DATA_INTERFACE_DESIGN §3.3)."""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

CLIENT_QUEUE_SIZE = 100
CLOSE = object()   # sentinel: the stream generator returns when it sees this


class Subscription:
    __slots__ = ("queue",)

    def __init__(self, maxsize: int) -> None:
        # items: (event_name, json_data, numbered) or CLOSE
        self.queue: asyncio.Queue = asyncio.Queue(maxsize)


class Broadcaster:
    def __init__(self, queue_size: int = CLIENT_QUEUE_SIZE) -> None:
        self._subs: set[Subscription] = set()
        self._queue_size = queue_size

    @property
    def client_count(self) -> int:
        return len(self._subs)

    def subscribe(self) -> Subscription:
        sub = Subscription(self._queue_size)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    def publish(self, event: str, data: str, *, numbered: bool = False) -> None:
        """`data` is serialized once by the caller and shared by every client."""
        item = (event, data, numbered)
        for sub in list(self._subs):
            try:
                sub.queue.put_nowait(item)
            except asyncio.QueueFull:
                self._evict(sub)

    def close_all(self) -> None:
        for sub in list(self._subs):
            self._evict(sub, reason="shutdown")

    def _evict(self, sub: Subscription, reason: str = "slow client") -> None:
        """Close on overflow: the client reconnects and gets a fresh snapshot (decision D2)."""
        self._subs.discard(sub)
        while not sub.queue.empty():
            sub.queue.get_nowait()
        sub.queue.put_nowait(CLOSE)
        log.info("SSE client closed (%s)", reason)


def format_sse(event: str, data: str, event_id: int | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"
```

- Every event is serialized **once** by `MarketService` and the same string is queued for every client (DATA_INTERFACE_DESIGN §3.3).
- Queue size is 100 events, which is about 50 s of ticks. A client that stops reading for that long is disconnected (decision D2). `EventSource` reconnects after the `retry: 1000` hint and receives a full `snapshot`, so no client is ever left with a gap.
- Shutdown calls `close_all()`, so every streaming response ends and uvicorn can exit without waiting for its graceful-shutdown timeout.

### 9.2 Route

```python
"""GET /api/stream/prices (API.md 'SSE price stream')."""
from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.market.broadcaster import CLOSE, format_sse
from app.market.service import MarketService

router = APIRouter()


async def sse_events(market: MarketService) -> AsyncIterator[str]:
    # subscribe() and snapshot_data() run with no await between them, so no update
    # can slip between the snapshot and the first queued `prices` event.
    sub = market.broadcaster.subscribe()
    snapshot = market.snapshot_data()
    seq = 0
    try:
        yield "retry: 1000\n\n"
        yield format_sse("snapshot", snapshot)
        while True:
            item = await sub.queue.get()
            if item is CLOSE:
                return                      # overflow or shutdown: EventSource reconnects
            event, data, numbered = item
            if numbered:
                seq += 1
                yield format_sse(event, data, seq)
            else:
                yield format_sse(event, data)
    finally:
        market.broadcaster.unsubscribe(sub)


@router.get("/api/stream/prices")
async def stream_prices(request: Request) -> StreamingResponse:
    market: MarketService = request.app.state.market
    return StreamingResponse(
        sse_events(market),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

- **No gap, no duplicate:** `subscribe()` and `snapshot_data()` run one after the other with no `await` between them, so the driver task cannot tick in between. Every update after the snapshot is in the queue, and no update in the queue is already in the snapshot.
- `id:` is a per-connection counter on `prices` events only (API.md). `heartbeat` and `snapshot` carry no `id`.
- `X-Accel-Buffering: no` is harmless locally and prevents buffering if the app is ever put behind nginx.
- Starlette cancels the generator when the client disconnects, and `finally` unsubscribes it.
- The route sits behind the app-wide Host allowlist middleware (API.md conventions). It has no body, so the `415` rule does not apply.

### 9.3 Example stream

Real output from the code above (simulator, `SIM_SEED`, jumps off, AAPL and NVDA, two ticks before connecting):

```
retry: 1000

event: snapshot
data: {"source":"simulator","source_status":"ok","tickers":{"AAPL":{"price":230.15,"previous_price":230.04,"prev_close":230.0,"change_pct":0.07,"direction":"up","timestamp":"2026-11-18T13:53:20.500Z","history":[[1795010000000,230.04],[1795010000500,230.15]]},"NVDA":{"price":130.16,"previous_price":130.13,"prev_close":130.0,"change_pct":0.12,"direction":"up","timestamp":"2026-11-18T13:53:20.500Z","history":[[1795010000000,130.13],[1795010000500,130.16]]}}}

event: prices
id: 1
data: [{"ticker":"AAPL","price":230.06,"previous_price":230.15,"prev_close":230.0,"change_pct":0.03,"direction":"down","timestamp":"2026-11-18T13:53:21.000Z"},{"ticker":"NVDA","price":130.13,"previous_price":130.16,"prev_close":130.0,"change_pct":0.1,"direction":"down","timestamp":"2026-11-18T13:53:21.000Z"}]

event: prices
id: 2
data: [{"ticker":"AAPL","price":230.46,"previous_price":230.06,"prev_close":230.0,"change_pct":0.2,"direction":"up","timestamp":"2026-11-18T13:53:21.500Z"},{"ticker":"NVDA","price":130.26,"previous_price":130.13,"prev_close":130.0,"change_pct":0.2,"direction":"up","timestamp":"2026-11-18T13:53:21.500Z"}]

event: heartbeat
data: {"ts":"2026-11-18T13:53:25Z","source_status":"ok","consecutive_failures":0}
```

A Massive-mode heartbeat after three failed polls:

```
event: heartbeat
data: {"ts":"2026-11-18T14:31:05Z","source_status":"degraded","consecutive_failures":3}
```

**Payload size:** a full snapshot is about 25 bytes per history point. That is about 15 KB per ticker, or about 150 KB for the default 10 tickers. It is sent once per connection.

---

## 10. MarketService — `service.py`

### 10.1 Implementation

```python
"""MarketService: owns the source, cache, broadcaster and the 500 ms driver loop."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterable, Mapping

from .broadcaster import Broadcaster
from .cache import PriceCache
from .source import PriceSource
from .types import StoredPrice, iso_s, now_ms

log = logging.getLogger(__name__)

TICK_INTERVAL_S = 0.5
HEARTBEAT_INTERVAL_S = 5.0


def _dumps(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":"), allow_nan=False)


class MarketService:
    def __init__(
        self,
        source: PriceSource,
        *,
        cache: PriceCache | None = None,
        broadcaster: Broadcaster | None = None,
        tick_interval_s: float = TICK_INTERVAL_S,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self.source = source
        self.cache = cache or PriceCache()
        self.broadcaster = broadcaster or Broadcaster()
        self._tick_s = tick_interval_s
        self._hb_s = heartbeat_interval_s
        self._clock = clock
        self._members_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    @property
    def source_name(self) -> str:
        return self.source.name

    # --- lifecycle --------------------------------------------------------------
    async def bootstrap(self, tickers: Iterable[str], stored: Mapping[str, StoredPrice]) -> None:
        """Before start(): register persisted members (no validation) and restore prices."""
        for t in sorted(set(tickers)):
            await self.source.add_ticker(t)
            self.cache.add_member(t)
        self.cache.restore(stored)

    async def start(self) -> None:
        await self.source.start()
        self._task = asyncio.create_task(self._run(), name="market-driver")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.source.stop()
        self.broadcaster.close_all()

    # --- membership: watchlist ∪ held (PLAN §6) --------------------------------------
    async def ensure(self, ticker: str) -> None:
        """Start pricing. Raises UnknownTicker / SourceUnavailable from the source."""
        async with self._members_lock:
            if ticker in self.cache.members:
                return
            await self.source.add_ticker(ticker)
            self.cache.add_member(ticker)

    async def release(self, ticker: str) -> None:
        """Stop pricing. Caller checks that the ticker is neither watched nor held."""
        async with self._members_lock:
            if ticker not in self.cache.members:
                return
            self.cache.remove_member(ticker)
            await self.source.remove_ticker(ticker)

    async def set_members(self, tickers: Iterable[str]) -> None:
        """Make membership exactly `tickers` (used by Reset). Unknown adds are logged."""
        wanted = set(tickers)
        for t in sorted(self.cache.members - wanted):
            await self.release(t)
        for t in sorted(wanted - self.cache.members):
            try:
                await self.ensure(t)
            except Exception as e:      # noqa: BLE001 - reset must not fail on one ticker
                log.warning("Could not price %s after reset: %s", t, e)

    # --- prices for other modules -------------------------------------------------
    def fill_price(self, ticker: str) -> float | None:
        """Price for a market order (PLAN §8 fill rule). Live prices only: a value
        restored from price_state is not fillable (-> 409 price_unavailable)."""
        s = self.cache.get(ticker)
        return s.price if s is not None and s.price is not None and not s.stale else None

    def valuation_price(self, ticker: str) -> tuple[float, bool] | None:
        """(price, stale) for portfolio valuation and snapshots; None = no price at all."""
        s = self.cache.get(ticker)
        if s is None or s.price is None:
            return None
        return s.price, s.stale

    def export_price_state(self) -> list[StoredPrice]:
        return self.cache.export_price_state()

    # --- SSE payloads ---------------------------------------------------------------
    def snapshot_data(self) -> str:
        h = self.source.health
        return _dumps({
            "source": self.source.name,
            "source_status": h.status,
            "tickers": self.cache.snapshot_tickers(),
        })

    def heartbeat_data(self) -> str:
        h = self.source.health
        return _dumps({
            "ts": iso_s(self._clock()),
            "source_status": h.status,
            "consecutive_failures": h.consecutive_failures,
        })

    # --- the driver loop -------------------------------------------------------------
    def step(self) -> int:
        """One tick: pull from the source, apply to the cache, publish changes.
        Synchronous, so a test can drive it without timers. Returns #changed."""
        changed = self.cache.apply(self.source.tick())
        if changed:
            self.broadcaster.publish(
                "prices", _dumps([s.to_wire(with_history=False) for s in changed]), numbered=True
            )
        return len(changed)

    def beat(self) -> None:
        self.broadcaster.publish("heartbeat", self.heartbeat_data())

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_tick = next_beat = loop.time()
        while True:
            try:
                self.step()
                if loop.time() >= next_beat:
                    self.beat()
                    next_beat += self._hb_s
            except Exception:                       # the loop must never die
                log.exception("market tick failed")
            next_tick += self._tick_s
            delay = next_tick - loop.time()
            if delay < 0:                           # fell behind (e.g. GC pause): resync
                next_tick, delay = loop.time(), 0.0
            await asyncio.sleep(delay)
```

### 10.2 Who calls what

The cache must cover watchlist ∪ held (PLAN §6). The market module does not read the database. The modules that change the watchlist or positions call the service:

| Event | Caller | Call | Notes |
|---|---|---|---|
| Startup | lifespan (§11.2) | `bootstrap(watchlist ∪ held, price_state)` then `start()` | no validation calls |
| `POST /api/watchlist` | watchlist route | `await market.ensure(t)` **before** the DB insert | `UnknownTicker` → `404 unknown_ticker`, nothing is written |
| `DELETE /api/watchlist/{t}` | watchlist route | after the DB delete: `release(t)` if `t` is not held | a held ticker keeps streaming (API.md) |
| Sell closes a position | trade service, after commit | `release(t)` if `t` is not on the watchlist | |
| Buy | trade service | nothing | a ticker must already be priced to be bought (§14 #5) |
| Reset | reset route, inside the trade lock | `set_members(DEFAULT_WATCHLIST)` | prices continue (PLAN §8) |
| Chat actions | same code paths as above | | AGENTS.md: one code path |

### 10.3 Prices for other modules

| Method | Returns | Used by |
|---|---|---|
| `fill_price(t)` | live price, or `None` if unpriced **or stale** | trade execution → `409 price_unavailable` |
| `valuation_price(t)` | `(price, stale)` or `None` | `GET /api/portfolio` (`stale` flag), snapshot task, LLM context |
| `export_price_state()` | `list[StoredPrice]` | snapshot task every 30 s, and shutdown |

---

## 11. Source Selection and App Wiring

### 11.1 Factory — `factory.py`

```python
"""The only place that branches on the market source (PLAN §5)."""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from .massive import MassiveSource
from .simulator import JumpModel, SimulatorSource
from .source import PriceSource
from .types import StoredPrice

log = logging.getLogger(__name__)


def _num(env: Mapping[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        log.warning("Ignoring invalid %s=%r; using %s", key, raw, default)
        return default


def make_price_source(
    env: Mapping[str, str] = os.environ,
    *,
    stored: Mapping[str, StoredPrice] | None = None,
) -> PriceSource:
    key = (env.get("MASSIVE_API_KEY") or "").strip()
    if key:
        return MassiveSource(
            key,
            poll_interval_s=_num(env, "MASSIVE_POLL_INTERVAL_SECONDS", 15.0),
            calls_per_minute=int(_num(env, "MASSIVE_CALLS_PER_MINUTE", 5)),
        )
    events = (env.get("SIM_EVENTS_ENABLED") or "true").strip().lower() != "false"
    restored = {t: (s.price, s.prev_close) for t, s in (stored or {}).items()}
    return SimulatorSource(jumps=JumpModel() if events else None, restored=restored)
```

| Env | Effect |
|---|---|
| `MASSIVE_API_KEY` non-empty | `MassiveSource` |
| `MASSIVE_POLL_INTERVAL_SECONDS` | configured interval (default 15). It may be stretched (§7.2). |
| `MASSIVE_CALLS_PER_MINUTE` | token bucket size (default 5) |
| `SIM_EVENTS_ENABLED=false` | simulator without jumps (PLAN §6; tests) |

`SIM_SEED` stays a code constant (PLAN §6). Unit tests pass `rng_seed=` directly. Making it an env var for E2E is REVIEW M7, listed in §14 #3.

### 11.2 Lifespan (backend agent's `main.py`, market parts only)

```python
from contextlib import asynccontextmanager
import os

from fastapi import FastAPI

from app.api.stream import router as stream_router
from app.market.factory import make_price_source
from app.market.service import MarketService
from app.store import repo   # backend agent's repository layer


@asynccontextmanager
async def lifespan(app: FastAPI):
    await repo.init()                                      # schema + seed (PLAN §7)
    stored = {s.ticker: s for s in await repo.load_price_state()}
    members = set(await repo.watchlist_tickers()) | set(await repo.held_tickers())

    market = MarketService(make_price_source(os.environ, stored=stored))
    await market.bootstrap(members, stored)                # trusted adds, stale restore
    await market.start()
    app.state.market = market
    try:
        yield
    finally:
        await market.stop()
        await repo.save_price_state(market.export_price_state())   # clean shutdown (PLAN §7)


app = FastAPI(lifespan=lifespan)
app.include_router(stream_router)
```

### 11.3 Watchlist add (shows the error mapping)

```python
@router.post("/api/watchlist", status_code=201)
async def add_to_watchlist(body: AddTicker, request: Request):
    market: MarketService = request.app.state.market
    ticker = normalize_ticker(body.ticker)            # InvalidTicker -> 400 invalid_request
    if await repo.watchlist_contains(ticker):
        raise ApiError(409, "already_in_watchlist", f"{ticker} is already on the watchlist.")
    try:
        await market.ensure(ticker)                   # validates in Massive mode
    except UnknownTicker:
        raise ApiError(404, "unknown_ticker", f"No market data for {ticker}.")
    added_at = await repo.watchlist_add(ticker)
    return {"ticker": ticker, "added_at": added_at}
```

### 11.4 Trade fill (inside the trade lock)

```python
async with trade_lock:
    price = market.fill_price(ticker)
    if price is None:
        raise ApiError(409, "price_unavailable", f"No live price for {ticker} yet; nothing was filled.")
    ...  # amount_cents = round_half_up(quantity_e4 * price / 100), write trade + position + snapshot
```

### 11.5 Snapshot task (portfolio module, market parts only)

```python
async def snapshot_once(market: MarketService) -> None:
    async with trade_lock:                               # consistent cash + positions (REVIEW M11)
        cash_cents, positions = await repo.cash_and_positions()
        total = Decimal(cash_cents)
        for p in positions:
            priced = market.valuation_price(p.ticker)
            if priced is None:                           # PLAN §7: skip, never record zero
                log.warning("Snapshot skipped: no price for %s", p.ticker)
                break
            price, _stale = priced
            total += Decimal(p.quantity_e4) * Decimal(str(price)) / 100
        else:
            await repo.insert_snapshot(int(total.quantize(Decimal(1), ROUND_HALF_UP)))
    await repo.save_price_state(market.export_price_state())
    await repo.prune_snapshots(older_than_days=7)
```

### 11.6 Health

`GET /api/health` reports `market_source = request.app.state.market.source_name` (`"simulator"` or `"massive"`).

---

## 12. Behaviour Walkthroughs

**Fresh start, simulator.** Seeded DB, empty `price_state`. `bootstrap` registers 10 tickers at seed prices. The first tick (≤ 500 ms) produces 10 updates, all "changed" (first live), and one `prices` event. A browser that connects before that tick receives a `snapshot` with `price: null` for every ticker. One that connects after it receives prices with 1-point histories. After 5 minutes every history holds 600 points.

**Restart, simulator.** `price_state` holds AAPL 231.10 / prev_close 230.00. The cache restores AAPL as `stale`, the simulator resumes from 231.10, and the first tick clears `stale`. Unrealized P&L and the day change are continuous (PLAN §7, H8).

**Restart, Massive.** `price_state` holds AAPL. `GET /api/portfolio` returns AAPL with `stale: true`. A buy of AAPL returns `409 price_unavailable` until the first poll lands (≤ 15 s), after which it fills at the live price.

**User adds PYPL (Massive).** `ensure("PYPL")` → `add_ticker` → validation waits for a `VALIDATE` token (polls go first) → one snapshot request with `tickers=PYPL` → found → the price is buffered → `201` → PYPL appears in the next `prices` event, at most 500 ms later.

**LLM adds PYPL, SHOP, ZZZZ in one turn.** The chat executor calls `ensure` for each in order. The three calls arrive while the first one waits for a token, so they share one request (`tickers=PYPL,SHOP,ZZZZ`). PYPL and SHOP are applied, and ZZZZ is rejected with `unknown_ticker` in the per-item results.

**Massive outage.** Polls 1–3 get 503. Prices freeze, and `consecutive_failures` goes 1→2→3. The next heartbeat says `degraded`, and the dot turns yellow (PLAN §10). Poll 4 succeeds, so the count goes to 0, the heartbeat says `ok`, and the dot turns green.

**Slow browser tab.** A background tab that stops reading for more than 50 s is evicted. When it resumes, `EventSource` reconnects and the fresh `snapshot` replaces its state.

---

## 13. Tests

Layout: `backend/tests/market/`. All statistical tests run with jumps off, except the jump test, which sets σ = 0 (PLAN §12). Runtime for the whole file set is about 3 s.

Result of the run against the code in this document (Python 3.12.3, pytest-asyncio auto mode):

```
................................................                         [100%]
48 passed in 3.39s
```

| PLAN §12 item | Test |
|---|---|
| Prices positive and finite | `test_gbm_step_is_positive_for_extreme_shock`, contract test |
| Drift exact (σ = 0, 1e-12) | `test_drift_exact_with_zero_sigma` |
| Diffusion variance within 2% (100k steps) | `test_diffusion_variance_within_2pct` |
| Itô term power check | `test_ito_term_has_power` |
| Drift power check, rejects μ = 0 and μ = −1 | `test_drift_has_power` |
| Correlation within 0.02 of β_iβ_j | `test_correlation_matches_beta_product` |
| Jump count, size, sign | `test_jump_count_size_and_sign` |
| Unknown symbol defaults | `test_unknown_symbol_uses_default_params` |
| Reproducible with seed | `test_same_seed_same_path` |
| Restart from `price_state` | `test_restart_resumes_from_price_state`, `test_restore_marks_stale_until_live` |
| Massive: parsing | `test_parse_*`, `test_timestamp_units` |
| Massive: chunking at 250 | `test_chunking_at_250` |
| Massive: 429/5xx keep prices, do not raise | `test_transient_errors_keep_prices_and_degrade_after_3` |
| Massive: `UnknownTicker` on not-found | `test_unknown_ticker_all_three_shapes` |
| Shared `PriceSource` contract | `test_price_source_contract[simulator, massive]` |
| SSE order and shapes | `test_stream_order_snapshot_prices_heartbeat` |

Statistical power, for review. Itô test: σ = 0.8, Δt = 1/252, N = 100k. The term is −1.27 × 10⁻³ and the SE is 1.59 × 10⁻⁴, so the term is 8 SE. Drift test: μ = 1, σ = 0.2. The SE is 4 × 10⁻⁵, while μ = 0 differs by 3.97 × 10⁻³, which is about 100 SE. Jump test: N = 240k, so the expected count is 200 ± 14.1, checked within 4 SD.

### 13.1 `test_simulator.py`

```python
import asyncio
import math
import statistics

import pytest

from app.market.seed_prices import DEFAULT_PARAMS, SeedParams
from app.market.simulator import DT_YEARS, JumpModel, SimulatorSource, gbm_step

N = 100_000


def make_sim(params, *, dt=DT_YEARS, seed=7, jumps=None):
    sim = SimulatorSource(rng_seed=seed, params=params, dt_years=dt, jumps=jumps, clock=lambda: 0)
    for t in params:
        asyncio.run(sim.add_ticker(t))
    return sim


def log_returns(sim, ticker, n=N):
    out, prev = [], sim.raw_price(ticker)
    for _ in range(n):
        sim.tick()
        cur = sim.raw_price(ticker)
        out.append(math.log(cur / prev))
        prev = cur
    return out


def test_drift_exact_with_zero_sigma():
    sim = make_sim({"X": SeedParams(100.0, 0.05, 0.0, 0.5)})
    r = log_returns(sim, "X", 1)[0]
    assert abs(r - 0.05 * DT_YEARS) < 1e-12


def test_diffusion_variance_within_2pct():
    sigma = 0.25
    sim = make_sim({"X": SeedParams(100.0, 0.0, sigma, 0.5)})
    var = statistics.variance(log_returns(sim, "X"))
    assert abs(var / (sigma**2 * DT_YEARS) - 1) < 0.02


def test_ito_term_has_power():
    mu, sigma, dt = 0.0, 0.8, 1 / 252
    sim = make_sim({"X": SeedParams(100.0, mu, sigma, 0.5)}, dt=dt)
    r = log_returns(sim, "X")
    mean, se = statistics.fmean(r), sigma * math.sqrt(dt) / math.sqrt(N)
    assert abs(mean - (mu - sigma**2 / 2) * dt) < 4 * se   # correct model passes
    assert abs(mean - mu * dt) > 4 * se                     # dropping -sigma^2/2 would fail


def test_drift_has_power():
    sigma, dt = 0.2, 1 / 252
    sim = make_sim({"X": SeedParams(100.0, 1.0, sigma, 0.5)}, dt=dt)
    r = log_returns(sim, "X")
    mean, se = statistics.fmean(r), sigma * math.sqrt(dt) / math.sqrt(N)
    expected = lambda mu: (mu - sigma**2 / 2) * dt
    assert abs(mean - expected(1.0)) < 4 * se
    assert abs(mean - expected(0.0)) > 4 * se
    assert abs(mean - expected(-1.0)) > 4 * se


def test_correlation_matches_beta_product():
    sim = make_sim({"A": SeedParams(100, 0.0, 0.3, 0.7), "B": SeedParams(100, 0.0, 0.3, 0.6)})
    ra, rb, pa, pb = [], [], sim.raw_price("A"), sim.raw_price("B")
    for _ in range(N):
        sim.tick()
        a, b = sim.raw_price("A"), sim.raw_price("B")
        ra.append(math.log(a / pa)); rb.append(math.log(b / pb)); pa, pb = a, b
    assert abs(statistics.correlation(ra, rb) - 0.7 * 0.6) < 0.02


def test_jump_count_size_and_sign():
    n, p = 240_000, 1 / 1200
    sim = make_sim({"X": SeedParams(100.0, 0.0, 0.0, 0.5)}, jumps=JumpModel(prob=p))
    jumps, prev = [], sim.raw_price("X")
    for _ in range(n):
        sim.tick()
        cur = sim.raw_price("X")
        if abs(cur / prev - 1) > 1e-12:
            jumps.append(cur / prev - 1)
        prev = cur
    mean, sd = n * p, math.sqrt(n * p * (1 - p))
    assert abs(len(jumps) - mean) < 4 * sd
    assert all(0.02 - 1e-12 <= abs(j) <= 0.05 + 1e-12 for j in jumps)
    ups = sum(j > 0 for j in jumps)
    assert abs(ups - len(jumps) / 2) < 4 * math.sqrt(len(jumps) / 4)


async def test_unknown_symbol_uses_default_params():
    sim = SimulatorSource(params={}, jumps=None, clock=lambda: 0)
    await sim.add_ticker("ZZZZ")
    [u] = sim.tick()
    assert DEFAULT_PARAMS == SeedParams(100.0, 0.05, 0.25, 0.5)
    assert u.prev_close == 100.0 and abs(u.price - 100.0) < 1.0


async def test_same_seed_same_path():
    async def path():
        sim = SimulatorSource(rng_seed=1, clock=lambda: 0)
        for t in ("AAPL", "MSFT"):
            await sim.add_ticker(t)
        return [[u.price for u in sim.tick()] for _ in range(500)]
    assert await path() == await path()


async def test_restart_resumes_from_price_state():
    sim = SimulatorSource(restored={"AAPL": (123.45, 120.00)}, jumps=None, clock=lambda: 0)
    await sim.add_ticker("AAPL")
    [u] = sim.tick()
    assert abs(u.price - 123.45) < 1.0 and u.prev_close == 120.00


def test_gbm_step_is_positive_for_extreme_shock():
    assert gbm_step(1.0, 0.0, 5.0, 1.0, -50.0) > 0
```

### 13.2 `test_cache.py`

```python
from app.market.cache import HISTORY_LEN, PriceCache
from app.market.types import PriceUpdate, StoredPrice


def test_first_update_is_flat_and_changed():
    c = PriceCache(); c.add_member("AAPL")
    [s] = c.apply([PriceUpdate("AAPL", 100.0, 1, 99.0)])
    assert s.direction == "flat" and s.change_pct == 1.01


def test_direction_previous_price_and_history_cap():
    c = PriceCache(); c.add_member("AAPL")
    for i in range(HISTORY_LEN + 50):
        c.apply([PriceUpdate("AAPL", 100.0 + i, i, 100.0)])
    s = c.get("AAPL")
    assert s.direction == "up" and s.previous_price == s.price - 1
    assert len(s.history) == HISTORY_LEN and s.history[0] == (50, 150.0)


def test_unchanged_price_not_broadcast_but_recorded():
    c = PriceCache(); c.add_member("X")
    c.apply([PriceUpdate("X", 10.0, 1, 10.0)])
    assert c.apply([PriceUpdate("X", 10.0, 2, 10.0)]) == []
    assert len(c.get("X").history) == 2 and c.get("X").direction == "flat"


def test_duplicate_observation_is_noop():
    c = PriceCache(); c.add_member("X")
    c.apply([PriceUpdate("X", 10.0, 1, 9.0)])
    c.apply([PriceUpdate("X", 10.0, 1, 9.0)])
    assert len(c.get("X").history) == 1


def test_none_prev_close_never_erases():
    c = PriceCache(); c.add_member("X")
    c.apply([PriceUpdate("X", 10.0, 1, 9.0)])
    c.apply([PriceUpdate("X", 11.0, 2, None)])
    assert c.get("X").prev_close == 9.0


def test_non_member_updates_ignored():
    c = PriceCache()
    assert c.apply([PriceUpdate("X", 1.0, 1)]) == []


def test_restore_marks_stale_until_live():
    c = PriceCache(); c.add_member("X"); c.add_member("Y")
    c.restore({"X": StoredPrice("X", 50.0, 49.0, 1000)})
    assert c.get("X").stale and c.get("X").history == type(c.get("X").history)()
    assert c.get("Y").price is None
    [s] = c.apply([PriceUpdate("X", 50.0, 2000, 49.0)])   # same price, still "changed"
    assert not s.stale


def test_snapshot_lists_unpriced_with_nulls():
    c = PriceCache(); c.add_member("NEW")
    assert c.snapshot_tickers()["NEW"] == {
        "price": None, "previous_price": None, "prev_close": None,
        "change_pct": None, "direction": None, "timestamp": None, "history": [],
    }
```

### 13.3 `test_rate_limiter.py`

```python
import asyncio

from app.market.rate_limiter import Priority, TokenBucketLimiter


async def test_burst_then_priority_order():
    lim = TokenBucketLimiter(600)          # 10 tokens/s, burst 600
    lim._tokens = 0.0                      # start empty to force queuing
    order = []

    async def call(name, prio):
        await lim.acquire(prio)
        order.append(name)

    await asyncio.gather(call("baseline", Priority.BASELINE),
                         call("validate", Priority.VALIDATE),
                         call("poll", Priority.POLL))
    assert order == ["poll", "validate", "baseline"]
    await lim.aclose()


async def test_rate_is_respected():
    lim = TokenBucketLimiter(1200)         # 20/s
    lim._tokens = 0.0
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    for _ in range(5):
        await lim.acquire(Priority.POLL)
    assert loop.time() - t0 >= 0.2         # 5 tokens at 20/s
    await lim.aclose()


async def test_cancelled_waiter_does_not_consume():
    lim = TokenBucketLimiter(600); lim._tokens = 0.0
    t = asyncio.create_task(lim.acquire(Priority.POLL))
    await asyncio.sleep(0); t.cancel()
    await asyncio.wait_for(lim.acquire(Priority.POLL), 1.0)
    await lim.aclose()
```

### 13.4 `test_massive.py`

```python
import asyncio

import httpx
import pytest

from app.market.massive import (
    DEGRADED_AFTER, MassiveSource, chunked, effective_poll_interval, parse_snapshot_row, to_epoch_ms,
)
from app.market.rate_limiter import TokenBucketLimiter
from app.market.types import SourceUnavailable, UnknownTicker

NOW = 1_763_476_200_000

AAPL_ROW = {
    "ticker": "AAPL",
    "day": {"c": 191.0}, "prevDay": {"c": 190.10},
    "lastTrade": {"p": 191.42, "t": 1_763_476_199_000_000_000},
    "updated": 1_763_476_199_500_000_000,
}


def snapshot(*rows):
    return {"status": "OK", "count": len(rows), "tickers": list(rows)}


def fake_massive(handler):
    """MassiveSource wired to an in-process handler; generous limiter so tests are fast."""
    return MassiveSource("test-key", transport=httpx.MockTransport(handler),
                         limiter=TokenBucketLimiter(6000), clock=lambda: NOW)


# --- pure parsing ------------------------------------------------------------------
def test_parse_prefers_last_trade_and_converts_ns():
    u = parse_snapshot_row(AAPL_ROW, NOW)
    assert (u.ticker, u.price, u.prev_close) == ("AAPL", 191.42, 190.10)
    assert u.timestamp_ms == 1_763_476_199_000


def test_parse_falls_back_to_minute_then_day_close():
    assert parse_snapshot_row({"ticker": "X", "min": {"c": 5.0}, "day": {"c": 6.0}}, NOW).price == 5.0
    assert parse_snapshot_row({"ticker": "X", "day": {"c": 6.0}}, NOW).price == 6.0


def test_parse_reset_window_rows():
    assert parse_snapshot_row({"ticker": "X", "day": {"c": 0}, "prevDay": {"c": 0}}, NOW) is None
    u = parse_snapshot_row({"ticker": "X", "day": {"c": 6.0}, "prevDay": {}}, NOW)
    assert u.prev_close is None and u.timestamp_ms == NOW


def test_parse_error_row_is_none():
    assert parse_snapshot_row({"ticker": "ZZZZ", "error": "NOT_FOUND"}, NOW) is None


@pytest.mark.parametrize("raw,ms", [(1_700_000_000, 1_700_000_000_000),
                                    (1_700_000_000_000, 1_700_000_000_000),
                                    (1_700_000_000_000_000, 1_700_000_000_000),
                                    (1_700_000_000_000_000_000, 1_700_000_000_000)])
def test_timestamp_units(raw, ms):
    assert to_epoch_ms(raw) == ms


def test_chunking_at_250():
    assert [len(c) for c in chunked([f"T{i}" for i in range(600)])] == [250, 250, 100]


def test_interval_stretch():
    assert effective_poll_interval(15, 10, 5) == 15          # 4 polls/min + 1 spare
    assert effective_poll_interval(15, 600, 5) == 45         # 3 chunks per poll
    assert effective_poll_interval(15, 10, 2) == 60


# --- polling, retry and degraded ---------------------------------------------------
async def test_poll_buffers_updates_for_tick():
    seen = {}
    def handler(req):
        seen["auth"] = req.headers["authorization"]
        seen["tickers"] = req.url.params["tickers"]
        return httpx.Response(200, json=snapshot(AAPL_ROW))
    src = fake_massive(handler)
    await src.add_ticker("AAPL")          # before start(): no request
    await src.poll_once()
    assert seen == {"auth": "Bearer test-key", "tickers": "AAPL"}
    [u] = src.tick()
    assert u.price == 191.42 and src.tick() == []
    await src.stop()


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_transient_errors_keep_prices_and_degrade_after_3(status):
    responses = [httpx.Response(200, json=snapshot(AAPL_ROW))] + \
                [httpx.Response(status, json={"status": "ERROR"})] * DEGRADED_AFTER + \
                [httpx.Response(200, json=snapshot(AAPL_ROW))]
    src = fake_massive(lambda req: responses.pop(0))
    await src.add_ticker("AAPL")
    await src.poll_once(); src.tick()
    for i in range(DEGRADED_AFTER):
        await src.poll_once()                  # must not raise
        assert src.tick() == []
    assert src.health.status == "degraded" and src.health.consecutive_failures == 3
    await src.poll_once()
    assert src.health.status == "ok" and src.health.consecutive_failures == 0
    await src.stop()


async def test_network_error_counts_as_failure():
    def handler(req):
        raise httpx.ConnectError("boom")
    src = fake_massive(handler)
    await src.add_ticker("AAPL")
    await src.poll_once()
    assert src.health.consecutive_failures == 1
    await src.stop()


# --- add_ticker validation -----------------------------------------------------------
@pytest.mark.parametrize("response", [
    httpx.Response(404, json={"status": "NOT_FOUND"}),
    httpx.Response(200, json=snapshot()),
    httpx.Response(200, json=snapshot({"ticker": "ZZZZ", "error": "NOT_FOUND"})),
])
async def test_unknown_ticker_all_three_shapes(response):
    src = fake_massive(lambda req: response)
    await src.start()
    with pytest.raises(UnknownTicker):
        await src.add_ticker("ZZZZ")
    await src.stop()


async def test_concurrent_adds_share_one_request():
    calls = []
    def handler(req):
        calls.append(req.url.params["tickers"])
        rows = [{**AAPL_ROW, "ticker": t} for t in req.url.params["tickers"].split(",") if t != "ZZZZ"]
        return httpx.Response(200, json=snapshot(*rows))
    src = fake_massive(handler)
    src._running = True                     # validate without starting the poller
    results = await asyncio.gather(src.add_ticker("PYPL"), src.add_ticker("SHOP"),
                                   src.add_ticker("ZZZZ"), return_exceptions=True)
    assert results[:2] == [None, None] and isinstance(results[2], UnknownTicker)
    assert calls == ["PYPL,SHOP,ZZZZ"]
    assert {u.ticker for u in src.tick()} == {"PYPL", "SHOP"}   # first prices ready
    src._running = False
    await src.stop()


async def test_validation_transport_failure_is_source_unavailable():
    src = fake_massive(lambda req: httpx.Response(503))
    src._running = True
    with pytest.raises(SourceUnavailable):
        await src.add_ticker("PYPL")
    src._running = False
    await src.stop()


# --- baseline fallback -----------------------------------------------------------------
async def test_missing_prevday_triggers_one_prev_call():
    paths = []
    def handler(req):
        paths.append(req.url.path)
        if req.url.path.endswith("/prev"):
            return httpx.Response(200, json={"resultsCount": 1, "results": [{"T": "NEWCO", "c": 9.5}]})
        return httpx.Response(200, json=snapshot({"ticker": "NEWCO", "day": {"c": 10.0}}))
    src = fake_massive(handler)
    await src.add_ticker("NEWCO")
    await src.poll_once()
    assert src.tick()[0].prev_close is None
    await asyncio.gather(*src._background)
    await src.poll_once()
    assert src.tick()[0].prev_close == 9.5
    assert paths.count("/v2/aggs/ticker/NEWCO/prev") == 1      # once per day
    await src.stop()
```

### 13.5 `test_driver.py`

```python
import asyncio

from app.market.broadcaster import CLOSE
from app.market.service import MarketService
from app.market.simulator import SimulatorSource


async def test_driver_loop_publishes_prices_and_heartbeats():
    m = MarketService(SimulatorSource(), tick_interval_s=0.05, heartbeat_interval_s=0.2)
    await m.bootstrap(["AAPL", "MSFT"], {})
    sub = m.broadcaster.subscribe()
    await m.start()
    await asyncio.sleep(0.5)
    events = [sub.queue.get_nowait()[0] for _ in range(sub.queue.qsize())]
    await m.stop()                       # stop() closes every subscriber with CLOSE
    assert sub.queue.get_nowait() is CLOSE
    assert events.count("prices") >= 5 and events.count("heartbeat") >= 2
```

### 13.6 `test_service_and_stream.py` (includes `FakePriceSource` and the shared contract test)

```python
import json

import httpx
import pytest

from app.api.stream import sse_events
from app.market.broadcaster import Broadcaster, CLOSE
from app.market.service import MarketService
from app.market.simulator import SimulatorSource
from app.market.source import PriceSource
from app.market.massive import MassiveSource
from app.market.types import PriceUpdate, StoredPrice, UnknownTicker


class FakePriceSource(PriceSource):
    """Scripted source for cache/broadcaster/SSE tests."""
    name = "simulator"

    def __init__(self):
        self.tickers, self.queued = set(), []
        self.unknown = {"ZZZZ"}

    async def start(self): ...
    async def stop(self): ...

    async def add_ticker(self, symbol):
        if symbol in self.unknown:
            raise UnknownTicker(symbol)
        self.tickers.add(symbol)

    async def remove_ticker(self, symbol):
        self.tickers.discard(symbol)

    def tick(self):
        out, self.queued = self.queued, []
        return out


def parse(chunk):
    fields = dict(line.split(": ", 1) for line in chunk.strip().split("\n"))
    return fields.get("event"), fields.get("id"), json.loads(fields["data"]) if "data" in fields else None


async def test_stream_order_snapshot_prices_heartbeat():
    src = FakePriceSource()
    m = MarketService(src, clock=lambda: 1_763_476_205_000)
    await m.bootstrap(["AAPL"], {})
    src.queued = [PriceUpdate("AAPL", 191.42, 1_763_476_200_000, 190.10)]
    m.step()                                         # history has 1 point before connect
    gen = sse_events(m)
    assert await gen.__anext__() == "retry: 1000\n\n"
    ev, _, snap = parse(await gen.__anext__())
    assert ev == "snapshot" and snap["source_status"] == "ok"
    assert snap["tickers"]["AAPL"]["history"] == [[1_763_476_200_000, 191.42]]
    assert snap["tickers"]["AAPL"]["change_pct"] == 0.69
    src.queued = [PriceUpdate("AAPL", 191.55, 1_763_476_200_500, 190.10)]
    m.step(); m.beat()
    ev, eid, data = parse(await gen.__anext__())
    assert (ev, eid) == ("prices", "1") and data[0]["direction"] == "up"
    assert data[0]["timestamp"] == "2025-11-18T14:30:00.500Z"
    ev, eid, hb = parse(await gen.__anext__())
    assert ev == "heartbeat" and eid is None and hb == {
        "ts": "2025-11-18T14:30:05Z", "source_status": "ok", "consecutive_failures": 0}
    await gen.aclose()
    assert m.broadcaster.client_count == 0


async def test_slow_client_is_closed():
    b = Broadcaster(queue_size=2)
    sub = b.subscribe()
    for _ in range(3):
        b.publish("prices", "[]", numbered=True)
    assert sub.queue.get_nowait() is CLOSE and b.client_count == 0


async def test_membership_and_fill_rules():
    src = FakePriceSource()
    m = MarketService(src)
    await m.bootstrap(["AAPL"], {"AAPL": StoredPrice("AAPL", 190.0, 189.0, 1)})
    assert m.fill_price("AAPL") is None                   # stale: not fillable
    assert m.valuation_price("AAPL") == (190.0, True)
    src.queued = [PriceUpdate("AAPL", 191.0, 2, 189.0)]; m.step()
    assert m.fill_price("AAPL") == 191.0
    with pytest.raises(UnknownTicker):
        await m.ensure("ZZZZ")
    await m.ensure("PYPL"); assert "PYPL" in src.tickers
    assert m.fill_price("PYPL") is None                    # before its first tick
    await m.set_members(["AAPL", "MSFT"])
    assert src.tickers == {"AAPL", "MSFT"} == set(m.cache.members)


def massive_echo():
    """Massive client whose fake server knows every symbol it is asked about."""
    def handler(req):
        rows = [{"ticker": t, "day": {"c": 10.0}, "prevDay": {"c": 9.0}}
                for t in req.url.params["tickers"].split(",")]
        return httpx.Response(200, json={"status": "OK", "tickers": rows})
    return MassiveSource("k", transport=httpx.MockTransport(handler))


async def updates(src):
    if isinstance(src, MassiveSource):
        await src.poll_once()
    return src.tick()


@pytest.mark.parametrize("make", [lambda: SimulatorSource(jumps=None), massive_echo])
async def test_price_source_contract(make):
    """Shared contract test (PLAN §12): both real sources behave the same way."""
    src = make()
    await src.add_ticker("AAPL")                       # bootstrap add, before start()
    await src.start()
    await src.add_ticker("PYPL")
    await src.add_ticker("PYPL")                       # idempotent
    await src.remove_ticker("NOPE")                    # unknown remove is a no-op
    ups = await updates(src)
    assert {u.ticker for u in ups} == {"AAPL", "PYPL"}
    assert all(u.price > 0 and u.timestamp_ms > 0 and u.prev_close for u in ups)
    await src.remove_ticker("PYPL")
    assert {u.ticker for u in await updates(src)} == {"AAPL"}
    assert src.health.status == "ok"
    await src.stop()
```

### 13.7 E2E hooks (test agent)

- **Simulator E2E:** set `SIM_EVENTS_ENABLED=false` for calmer prices. Assert heatmap colours relative to each position's displayed P&L sign, not absolute values (REVIEW M7b).
- **Degraded and quiet-stream scenarios (PLAN §12):** run a small fake Massive HTTP server in the test compose file. It serves `/v2/snapshot/...` from a fixture and switches to 503 on command. This needs `MASSIVE_BASE_URL` to be overridable in the E2E build, which is open item §14 #4.

---

## 14. Decisions and Open Items

### Decisions this design makes where the spec is silent (no contract change needed)

| # | Decision | Why |
|---|---|---|
| D1 | `MarketService` drives `tick()` every 500 ms for both sources. Massive buffers inside its own poll task. | Keeps PLAN's interface and gives one clock (REVIEW M2) |
| D2 | Adds before `start()` skip validation | Restart must not spend the Massive budget |
| D3 | Validation is batched and runs at a priority between polls and baselines | An LLM turn of 10 adds costs one call |
| D4 | Polls use at most `calls_per_minute − 1` calls per minute | Leaves room for validation and baselines |
| D5 | History is appended on every accepted update; exact duplicates are dropped | REVIEW M13; no padding from Massive re-polls |
| D6 | First live update for a ticker is always "changed", with `direction: flat` | The client learns about new tickers from `prices` |
| D7 | Slow SSE clients are disconnected, not throttled | Reconnect and snapshot give exact recovery (DATA_INTERFACE_DESIGN D2) |
| D8 | Simulator restores `prev_close` from `price_state` | Day change survives restart (SIMULATOR_RESEARCH §4) |
| D9 | Reported prices are rounded to cents in simulator mode; Massive prices are passed through | API.md allows more precision; display rounds |

### Proposed contract changes (need `API.md`/`PLAN.md` first)

1. **`SourceUnavailable` mapping.** When Massive cannot be reached during `POST /api/watchlist`, the code raises `SourceUnavailable`. API.md has no code for this. Proposal: `503 market_unavailable`, and a chat item rejected with the same code. Until then, map it to `500 internal_error`.
2. **Rebroadcast `snapshot` on membership change** (REVIEW M15). `MarketService.set_members` is the single place to add `self.broadcaster.publish("snapshot", self.snapshot_data())`. API.md currently says "once per connection", so this is not done. Meanwhile the frontend must: insert unknown tickers it sees in `prices`, remove a row itself after `DELETE`, and refetch after Reset.
3. **`SIM_SEED` as a test-only env var** (REVIEW M7a), so E2E runs are reproducible.
4. **`MASSIVE_BASE_URL` override for E2E only** (a fake server). PLAN §5 pins the URL as a constant. Proposal: allow an override only when `FINALLY_TEST_MODE=true`.
5. **Buying a ticker that is neither watched nor held** returns `409 price_unavailable` under PLAN §8, because nothing is pricing it. The LLM prompt should say "add to the watchlist first". The alternative, auto-adding on buy, would be a product decision.
6. **Fills at a stale (restored) price** are refused by `fill_price`. PLAN §8 says "latest price in the cache", which arguably includes a restored price. This design reads it as a *live* price. Confirm, or relax `fill_price` to accept stale prices.
7. **`MASSIVE_CALLS_PER_MINUTE` default.** MASSIVE_API_RESEARCH §4 notes that paid tiers have no REST limit and the free tier cannot serve snapshots, so the default of 5 only throttles paid users. The default stays 5, per PLAN §5, until the owner decides.
8. **API.md example hygiene.** The `history` epoch values in the API.md examples (`1763476200000`) are 2025-11-18, but the ISO timestamps next to them say 2026-11-18. Fixtures should use `1795010000000` (2026-11-18T13:53:20Z), as in §9.3.

### Spikes this design depends on

S1(b), S1(d), S1(e), auth, timestamp units, and plan fields. All of them only affect constants in `massive.py` and fixtures in `test_massive.py` (§7.6). S2 (LLM) is unrelated to market data.

---

**Related:** [PLAN.md](PLAN.md) §5–§8, §12 · [API.md](API.md) · [DATA_INTERFACE_DESIGN.md](DATA_INTERFACE_DESIGN.md) · [SIMULATOR_RESEARCH.md](SIMULATOR_RESEARCH.md) · [MASSIVE_API_RESEARCH.md](MASSIVE_API_RESEARCH.md) · [REVIEW.md](REVIEW.md)
