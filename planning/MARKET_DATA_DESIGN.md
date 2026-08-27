# Market Data Backend — Detailed Design

Implementation-ready design for FinAlly's market data subsystem: one unified
provider API, two implementations (GBM simulator and the Massive REST client),
a price cache, a polling engine, and the SSE endpoint the frontend consumes.

This document consolidates and supersedes three planning documents:

| Source document | What this design takes from it |
|---|---|
| `MARKET_INTERFACE.md` | Pull-based provider interface, cache/engine split, wiring |
| `MARKET_SIMULATOR.md` | GBM math, accelerated clock, two-factor correlation, ticker universe |
| `MASSIVE_API.md` | Endpoint reference, plan/rate-limit matrix, response shapes, auth |
| `REVIEW.md` | Eight findings against the above; every one is resolved here (§4) |

Every code block below was executed before being written down. Measured
numbers appear in §7.8 and §9.5; they come from running this exact code.

---

## Table of Contents

1. [Scope and Status](#1-scope-and-status)
2. [Architecture](#2-architecture)
3. [Module Layout](#3-module-layout)
4. [Contract Decisions](#4-contract-decisions)
5. [Types — `types.py`](#5-types)
6. [The Provider Interface — `provider.py`](#6-the-provider-interface)
7. [Simulator — `simulator.py`](#7-simulator)
8. [Massive Client — `massive.py`](#8-massive-client)
9. [Price Cache — `cache.py`](#9-price-cache)
10. [Price Engine — `engine.py`](#10-price-engine)
11. [Provider Selection — `factory.py`](#11-provider-selection)
12. [SSE Streaming — `stream.py`](#12-sse-streaming)
13. [Application Wiring](#13-application-wiring)
14. [Watchlist Coordination](#14-watchlist-coordination)
15. [Error Handling and Edge Cases](#15-error-handling-and-edge-cases)
16. [Testing Strategy](#16-testing-strategy)
17. [Configuration Summary](#17-configuration-summary)
18. [Migration from the Existing Implementation](#18-migration-from-the-existing-implementation)
19. [Implementation Checklist](#19-implementation-checklist)

---

## 1. Scope and Status

In scope: everything between "a ticker symbol" and "a price on the screen" —
the provider abstraction, both implementations, the cache, the background
poller, the SSE endpoint, and the two hooks the rest of the backend needs
(`current_price()` for trade fills, `validate_symbol()` for watchlist adds).

Out of scope, deliberately (`PLAN.md` does not need them): historical bars,
WebSocket streaming, order books, quotes/volume analytics, per-provider rate
limiters. §11 of `MARKET_INTERFACE.md` explains each omission; nothing here
reopens them.

**Relationship to the code already in the repo.** `backend/app/market/`
contains a working push-based implementation built from
`planning/archive/MARKET_DATA_DESIGN.md` (summarised in
`MARKET_DATA_SUMMARY.md`). This design replaces it in place — same package
path, different internals — for three reasons the old shape cannot deliver:

- **Daily change %.** `PLAN.md` §10 requires a daily change column in the
  watchlist and positions table. The old `PriceUpdate` carries only
  tick-over-tick change; there is no previous-close anywhere in it.
- **Free-tier Massive support.** The old client assumes snapshot access,
  which returns 403 on Stocks Basic (`MASSIVE_API.md` §3).
- **Testability.** Providers that own their own background tasks are hard to
  drive deterministically; a pull-based `fetch()` is a plain async function.

§18 maps every old name to its replacement. Existing tests in
`backend/tests/market/` are replaced alongside the modules they cover.

---

## 2. Architecture

```
                        create_provider()            reads MASSIVE_API_KEY
                               |
              +----------------+----------------+
              |                                 |
      SimulatorProvider                  MassiveProvider
      (no API key, GBM)                  (REST, snapshot or grouped)
              |                                 |
              +----------------+----------------+
                               |
                     MarketDataProvider
                     .fetch(tickers) -> {ticker: Quote}
                     .validate_ticker(ticker) -> bool
                               |
                          PriceEngine              one background task
                               |                   polls on provider.poll_interval
                               v
                          PriceCache               latest PriceTick per ticker
                               |                   + monotonic version counter
              +----------------+----------------+
              |                                 |
   GET /api/stream/prices                portfolio valuation,
   (SSE, pushes on change, ~500ms)       trade fills, chat context
```

Three splits carry the design:

- **Providers own no timers and no tasks.** They answer `fetch()` when asked.
  Timing lives in the engine, so both implementations stay trivial to test
  and neither can leak a task.
- **Poll cadence is independent of push cadence.** The engine polls as fast
  as the provider allows (0.5s simulated, 15s on a free Massive key); SSE
  pushes from the cache whenever the cache changes, checking every 500ms. A
  slow upstream never stalls the stream — prices go stale, not blank.
- **The cache is the only definition of "current price."** Trade execution,
  portfolio valuation and the LLM's context all read it, so a trade always
  fills at exactly the price the user was looking at.

---

## 3. Module Layout

```
backend/app/market/
├── __init__.py      # public exports — the only import surface for the rest of the app
├── types.py         # Quote, PriceTick, Direction, ChangeBasis
├── provider.py      # MarketDataProvider ABC + symbol normalisation
├── simulator.py     # SimulatorProvider, TickerSpec, UNIVERSE
├── massive.py       # MassiveProvider
├── cache.py         # PriceCache
├── engine.py        # PriceEngine background task
├── stream.py        # create_stream_router() — GET /api/stream/prices
└── factory.py       # create_provider()
```

```python
# backend/app/market/__init__.py
"""Market data: one interface, a simulator, a Massive REST client, an SSE feed."""

from .cache import PriceCache
from .engine import PriceEngine
from .factory import create_provider
from .massive import MassiveProvider
from .provider import MarketDataProvider, normalize_symbol
from .simulator import SimulatorProvider
from .stream import create_stream_router
from .types import ChangeBasis, Direction, PriceTick, Quote

__all__ = [
    "ChangeBasis",
    "Direction",
    "MarketDataProvider",
    "MassiveProvider",
    "PriceCache",
    "PriceEngine",
    "PriceTick",
    "Quote",
    "SimulatorProvider",
    "create_provider",
    "create_stream_router",
    "normalize_symbol",
]
```

Import boundary: the rest of the backend imports from `app.market` only.
Nothing outside the package imports `simulator` or `massive` directly, so
adding or swapping a provider touches one file (`factory.py`).

---

## 4. Contract Decisions

`REVIEW.md` raised eight findings against the source documents. Each is
resolved below; the resolution is what the code in this document implements.

| # | Finding | Resolution here |
|---|---|---|
| 1 | Grouped-daily fallback could burn 5 API calls per fetch | Lazy date resolution: **at most one HTTP call per fetch, zero while the session bar is cached** (§8.3) |
| 2 | "Unknown tickers are omitted" is a contract the simulator cannot honour | Contract narrowed to `set(result) ⊆ set(requested)`; symbol existence is a separate `validate_ticker()` called at watchlist-add time (§6, §14) |
| 3 | Session-rollover test asserted a lucky tolerance | Test asserts the property that matters — the close moved off the seed and tracks the price within $1 (§16.3) |
| 4 | `previous_close` silently meant two different things | Renamed to `reference` + explicit `basis` field (`"prev_close"` \| `"session_open"`), carried through to the SSE payload so the UI can label the column (§5) |
| 5 | `PriceCache.prune` was dead code | Wired: the engine prunes to the current ticker set on every poll (§10) |
| 6 | Poll cadence drifted by the fetch duration | Deadline-based sleep; measured 6 × 0.1s polls with a 0.05s fetch take 0.62s, not 0.9s (§10) |
| 7 | Test dependencies unstated | `pytest-asyncio` with `asyncio_mode = "auto"` (already configured); HTTP mocking uses `httpx.MockTransport`, so no `pytest-httpx` dependency (§16) |
| 8 | Unverified claim about free-tier grouped-bar delay | Marked inferred in §8.1; the code does not depend on it |

Two further decisions this design adds:

- **`fetch()` is order-independent.** The simulator sorts and de-duplicates
  its ticker list before drawing, so `fetch(["AAPL","JPM"])` and
  `fetch(["JPM","AAPL"])` produce identical paths from the same seed. Set
  iteration order was the determinism trap flagged in `MARKET_SIMULATOR.md`
  §8; sorting inside `fetch()` closes it at the source rather than relying on
  every caller.
- **The cache suppresses no-op updates.** A quote identical to the stored
  tick does not bump the version and does not re-push over SSE. This matters
  on a free Massive key, where the same end-of-day price repeats forever.

---

## 5. Types

**File: `backend/app/market/types.py`**

```python
"""Value types shared by every market data provider."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Direction = Literal["up", "down", "flat"]
ChangeBasis = Literal["prev_close", "session_open"]


@dataclass(frozen=True, slots=True)
class Quote:
    """A price observation for one ticker, as reported by a provider.

    `reference` is the price the day's change is measured against, and
    `basis` says which reference it is. Snapshot data and the simulator
    report the prior session's close; free-tier grouped bars can only report
    that session's open.
    """

    ticker: str
    price: float
    reference: float
    timestamp: datetime
    basis: ChangeBasis = "prev_close"

    @property
    def change(self) -> float:
        return self.price - self.reference

    @property
    def change_percent(self) -> float:
        if self.reference == 0:
            return 0.0
        return (self.price - self.reference) / self.reference * 100


@dataclass(frozen=True, slots=True)
class PriceTick:
    """A cached quote enriched with tick-over-tick movement for the UI."""

    ticker: str
    price: float
    previous_price: float
    reference: float
    timestamp: datetime
    basis: ChangeBasis
    direction: Direction

    @property
    def change(self) -> float:
        return self.price - self.reference

    @property
    def change_percent(self) -> float:
        if self.reference == 0:
            return 0.0
        return (self.price - self.reference) / self.reference * 100

    def to_dict(self) -> dict:
        """Wire format for SSE frames and REST responses."""
        return {
            "ticker": self.ticker,
            "price": round(self.price, 2),
            "previous_price": round(self.previous_price, 2),
            "reference": round(self.reference, 2),
            "basis": self.basis,
            "direction": self.direction,
            "change": round(self.change, 2),
            "change_percent": round(self.change_percent, 2),
            "timestamp": self.timestamp.isoformat(),
        }
```

Two "previous" values, because the UI needs two different things:

- `previous_price` is the immediately preceding tick. It drives `direction`,
  which drives the green/red flash animation.
- `reference` is the daily baseline. It drives the change % column in the
  watchlist and positions table, and `basis` tells the frontend whether to
  label that column "vs prev close" or "since open".

Providers produce `Quote`. `PriceTick` is assembled by the cache, the only
component that knows what the previous tick was. Frozen dataclasses with
`slots=True`: nothing downstream can mutate a price, and ten tickers ×
one tick each stays negligible.

---

## 6. The Provider Interface

**File: `backend/app/market/provider.py`**

```python
"""Abstract interface every market data source implements."""

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence

from .types import Quote

SYMBOL_RE = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")


class MarketDataProvider(ABC):
    """A pull-based source of current prices for a set of tickers.

    Implementations are cheap to construct and hold no background tasks. The
    caller decides when to fetch; `poll_interval` is the provider's advice on
    how often that should be, and may change at runtime (see MassiveProvider's
    free-tier downgrade), so read it fresh each iteration.
    """

    name: str
    poll_interval: float

    @abstractmethod
    async def fetch(self, tickers: Sequence[str]) -> dict[str, Quote]:
        """Return current quotes keyed by ticker.

        Contract:
          * every key is one of `tickers`; a provider never invents symbols
          * a ticker it cannot price is omitted, not raised and not faked
          * an empty `tickers` returns `{}` without doing any I/O
          * raises only on whole-request failures: network error, bad key
        """

    async def validate_ticker(self, ticker: str) -> bool:
        """True if this provider can price the symbol.

        Called once, when a ticker is added to the watchlist — never in the
        poll loop. The default accepts anything, which is correct for the
        simulator; MassiveProvider overrides it with a real lookup.
        """
        return True

    async def aclose(self) -> None:
        """Release held resources. Safe to call more than once."""


def normalize_symbol(raw: str) -> str | None:
    """Uppercase and syntax-check a user-supplied symbol. None if implausible."""
    symbol = raw.strip().upper()
    return symbol if SYMBOL_RE.match(symbol) else None
```

Design notes:

- **One fetch method.** Both backends are pull-based, so `fetch()` covers
  both. No `subscribe`/`unsubscribe` pair to keep in sync with the watchlist.
- **`tickers` is passed per call, not held as state.** The watchlist changes
  at runtime; passing it each call means there is nothing to resubscribe.
- **Validation is separate from fetching** (`REVIEW.md` finding 2). Fetching
  a symbol the provider cannot price is a non-event — it is simply absent
  from the result. Deciding whether a symbol is real is a one-off question
  asked when the user adds it, where an HTTP round trip is affordable and an
  error message is useful. `normalize_symbol` handles the cheap half of that
  question (typos like `aapl!`) without any provider call.
- **`aclose()` is a no-op by default.** The simulator holds nothing to
  release and does not override it.

---

## 7. Simulator

**File: `backend/app/market/simulator.py`**

The default source: plausible, correlated, always-positive price paths with
no network dependency and no API key.

### 7.1 Why GBM

The standard model for equity prices, right here for three reasons that all
show up on screen: prices stay positive (the step is multiplicative), returns
scale rather than prices (a $1 move looks the same on a $900 stock as it
should), and volatility is one interpretable dial.

The discrete exact solution, which is what the code implements:

```
S(t+dt) = S(t) * exp( (mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z )
```

`mu` is annualised drift, `sigma` annualised volatility, `dt` elapsed time in
years, `Z` a standard normal draw. The `- sigma^2/2` Ito correction is not
optional: without it the median path drifts below `mu` and high-volatility
names creep upward even at zero drift. Dropping it is the most common way to
get this wrong, and §16.3's volatility test is what catches it.

### 7.2 The accelerated clock

A real 500ms move is invisible: at 30% annual volatility a half-second move
on a $190 stock is about half a cent. So one tick represents one **minute**
of market time:

```
TRADING_MINUTES_PER_YEAR = 252 * 390 = 98_280
dt = tick_minutes / TRADING_MINUTES_PER_YEAR
```

At `tick_minutes=1.0`, `sigma=0.30`, the per-tick sigma is
`0.30 * sqrt(1/98_280) ≈ 0.096%` — about $0.18 on a $190 stock. Large enough
to flash, small enough to look like a price rather than a slot machine.

| Wall clock | Simulated |
|---|---|
| 500ms | 1 minute |
| 3.25 min | one full session (390 minutes) |
| 1 hour | about 18 trading days |

**Session rollover.** Every 390 ticks each ticker's `reference` is reset to
its current price. Without it, an app left running for an hour shows
"+38.2% today", which reads as broken. With it, the daily change stays in a
believable band no matter how long the demo runs.

### 7.3 Correlation

Independent draws look wrong — half the board green, half red, forever. A
two-factor decomposition gives correlation without matrix algebra:

```
Z_i = sqrt(w_m) * Z_market
    + sqrt(w_s) * Z_sector(i)
    + sqrt(1 - w_m - w_s) * Z_i
```

The weights sum to 1, so each `Z_i` keeps unit variance and `sigma` stays
exactly the volatility it claims to be. Same-sector pairs correlate at
`w_m + w_s`, cross-sector at `w_m`. Defaults `w_m=0.35`, `w_s=0.25` give
0.60 and 0.35 — inside the range US large caps actually exhibit, and
measured back at 0.597/0.354 in §7.8. One market draw, one draw per sector
present, one draw per ticker: no Cholesky decomposition, no covariance
matrix, no numpy.

### 7.4 Events

Each tick, each ticker independently jumps with probability
`event_probability`, multiplying the price by `1 ± u`, `u` uniform on
[0.02, 0.05]. At the default `0.0008` that is roughly one event per ticker
every 1250 ticks, so across ten tickers something dramatic happens about
once a minute of wall clock. Events are applied after the GBM step and
logged, so an unexplained spike on a chart is traceable in the server log.

### 7.5 The ticker universe

Deliberately round demo constants, not market data:

| Ticker | Seed price | Sector | Volatility | Drift |
|---|---|---|---|---|
| AAPL | 190.00 | tech | 0.28 | 0.10 |
| GOOGL | 175.00 | tech | 0.32 | 0.10 |
| MSFT | 420.00 | tech | 0.26 | 0.10 |
| NVDA | 880.00 | tech | 0.50 | 0.20 |
| META | 500.00 | tech | 0.38 | 0.12 |
| AMZN | 185.00 | consumer | 0.34 | 0.12 |
| TSLA | 250.00 | consumer | 0.55 | 0.10 |
| NFLX | 620.00 | consumer | 0.40 | 0.12 |
| JPM | 200.00 | financial | 0.24 | 0.06 |
| V | 280.00 | financial | 0.22 | 0.08 |

Five tech names against three consumer and two financial makes the sector
factor visible. TSLA and NVDA carry the highest volatility on purpose — a
watchlist where every row twitches identically looks synthetic.

Any other symbol gets a spec derived from a SHA-256 hash: price in
[20, 400], volatility in [0.20, 0.60], a sector. Hashing rather than
randomising means `PYPL` starts at $117.09 on every restart, which keeps the
app coherent across container restarts and keeps E2E tests stable. Python's
built-in `hash()` will not do — it is salted per process.

### 7.6 Code

```python
"""Geometric Brownian motion price simulator."""

import hashlib
import logging
import math
import os
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from .provider import MarketDataProvider
from .types import Quote

logger = logging.getLogger(__name__)

TRADING_MINUTES_PER_YEAR = 252 * 390
SESSION_TICKS = 390
SECTORS = ("tech", "consumer", "financial", "energy", "health")


@dataclass(frozen=True, slots=True)
class TickerSpec:
    """Static parameters governing one ticker's price path."""

    ticker: str
    seed_price: float
    sector: str
    volatility: float
    drift: float


UNIVERSE = {
    spec.ticker: spec
    for spec in (
        TickerSpec("AAPL", 190.00, "tech", 0.28, 0.10),
        TickerSpec("GOOGL", 175.00, "tech", 0.32, 0.10),
        TickerSpec("MSFT", 420.00, "tech", 0.26, 0.10),
        TickerSpec("NVDA", 880.00, "tech", 0.50, 0.20),
        TickerSpec("META", 500.00, "tech", 0.38, 0.12),
        TickerSpec("AMZN", 185.00, "consumer", 0.34, 0.12),
        TickerSpec("TSLA", 250.00, "consumer", 0.55, 0.10),
        TickerSpec("NFLX", 620.00, "consumer", 0.40, 0.12),
        TickerSpec("JPM", 200.00, "financial", 0.24, 0.06),
        TickerSpec("V", 280.00, "financial", 0.22, 0.08),
    )
}


def derive_spec(ticker: str) -> TickerSpec:
    """Build a stable spec for a ticker outside the seeded universe."""
    digest = hashlib.sha256(ticker.encode()).digest()
    price = 20.0 + int.from_bytes(digest[:4], "big") % 38_000 / 100
    volatility = 0.20 + digest[4] / 255 * 0.40
    return TickerSpec(
        ticker=ticker,
        seed_price=round(price, 2),
        sector=SECTORS[digest[5] % len(SECTORS)],
        volatility=round(volatility, 3),
        drift=0.08,
    )


class SimulatorProvider(MarketDataProvider):
    """Prices generated locally by correlated geometric Brownian motion.

    Each call to `fetch` advances the world by one tick, so the caller's poll
    interval sets the simulation's pace.
    """

    name = "simulator"

    def __init__(
        self,
        seed: int | None = None,
        poll_interval: float = 0.5,
        tick_minutes: float = 1.0,
        market_weight: float = 0.35,
        sector_weight: float = 0.25,
        event_probability: float = 0.0008,
        mean_reversion: float = 0.0,
    ):
        if seed is None and (env_seed := os.getenv("SIMULATOR_SEED")):
            seed = int(env_seed)
        self.poll_interval = poll_interval
        self._rng = random.Random(seed)
        self._dt = tick_minutes / TRADING_MINUTES_PER_YEAR
        self._market_weight = market_weight
        self._sector_weight = sector_weight
        self._own_weight = 1.0 - market_weight - sector_weight
        self._event_probability = event_probability
        self._mean_reversion = mean_reversion
        self._specs: dict[str, TickerSpec] = {}
        self._prices: dict[str, float] = {}
        self._closes: dict[str, float] = {}
        self._tick = 0

    async def fetch(self, tickers: Sequence[str]) -> dict[str, Quote]:
        """Advance one tick and return a quote for every requested ticker."""
        if not tickers:
            return {}
        ordered = sorted(dict.fromkeys(tickers))  # order-independent determinism
        for ticker in ordered:
            self._register(ticker)
        self._advance(ordered)
        now = datetime.now(timezone.utc)
        return {
            ticker: Quote(
                ticker=ticker,
                price=round(self._prices[ticker], 2),
                reference=round(self._closes[ticker], 2),
                timestamp=now,
                basis="prev_close",
            )
            for ticker in ordered
        }

    def _register(self, ticker: str) -> None:
        """Add a ticker to the simulation at its seed price if new."""
        if ticker in self._specs:
            return
        spec = UNIVERSE.get(ticker) or derive_spec(ticker)
        self._specs[ticker] = spec
        self._prices[ticker] = spec.seed_price
        self._closes[ticker] = spec.seed_price

    def _advance(self, tickers: Sequence[str]) -> None:
        """Step every requested ticker forward by one correlated tick."""
        self._tick += 1
        if self._tick % SESSION_TICKS == 0:
            self._closes.update(self._prices)
            logger.info("simulator: session %d closed", self._tick // SESSION_TICKS)

        market_shock = self._rng.gauss(0.0, 1.0)
        sector_shocks = {
            sector: self._rng.gauss(0.0, 1.0)
            for sector in sorted({self._specs[t].sector for t in tickers})
        }
        for ticker in tickers:
            spec = self._specs[ticker]
            shock = (
                math.sqrt(self._market_weight) * market_shock
                + math.sqrt(self._sector_weight) * sector_shocks[spec.sector]
                + math.sqrt(self._own_weight) * self._rng.gauss(0.0, 1.0)
            )
            self._prices[ticker] = self._step(spec, self._prices[ticker], shock)

    def _step(self, spec: TickerSpec, price: float, shock: float) -> float:
        """One GBM step for a ticker, plus an occasional event jump."""
        drift = spec.drift
        if self._mean_reversion:
            drift += self._mean_reversion * math.log(spec.seed_price / price)
        exponent = (drift - 0.5 * spec.volatility**2) * self._dt
        exponent += spec.volatility * math.sqrt(self._dt) * shock
        price *= math.exp(exponent)

        if self._rng.random() < self._event_probability:
            move = self._rng.choice((1, -1)) * self._rng.uniform(0.02, 0.05)
            price *= 1.0 + move
            logger.info("simulator: event on %s, %+.1f%%", spec.ticker, move * 100)

        return max(price, 0.01)
```

The simulator inherits `validate_ticker` (always True) and `aclose` (no-op)
from the base class. It prices anything, by design — that is the useful
behaviour for a demo, and §14 shows how the watchlist route stays consistent
across providers anyway.

### 7.7 Determinism

Same seed, same tickers, same sequence. Four things make that true, each easy
to break:

1. **One `random.Random` instance**, never module-level `random`, whose state
   is shared with everything else in the process.
2. **`sorted()` around the sector set** in `_advance`. Iterating a set of
   strings gives `PYTHONHASHSEED`-dependent order, which changes the order
   RNG draws are consumed and quietly destroys reproducibility.
3. **`sorted(dict.fromkeys(...))` in `fetch`**, so caller ordering and
   accidental duplicates cannot shift the draw sequence.
4. **`hashlib.sha256`, not `hash()`**, for derived specs.

Determinism holds for a fixed ticker set. Adding a ticker mid-run changes the
number of draws per tick, so paths diverge from that point on — expected, and
the reason tests fix their ticker list. Set the seed with `SIMULATOR_SEED` or
the constructor argument; leave it unset in production so each container gets
a different market.

### 7.8 Tuning and measured behaviour

| Parameter | Default | Effect |
|---|---|---|
| `poll_interval` | 0.5 | Wall-clock seconds per tick |
| `tick_minutes` | 1.0 | Simulated minutes per tick — the main pace dial |
| `market_weight` | 0.35 | Cross-sector correlation |
| `sector_weight` | 0.25 | Additional within-sector correlation |
| `event_probability` | 0.0008 | Per-ticker, per-tick jump chance |
| `mean_reversion` | 0.0 | Pull toward the seed price. Off by default |
| `seed` | None | Fix for reproducible paths |

Recipes: `tick_minutes=3.0` for a livelier demo (a session in ~65s);
`tick_minutes=0.25, event_probability=0.0` for calm realism;
`market_weight=0.8, sector_weight=0.15` to show a market-wide selloff in the
heatmap; `mean_reversion=10.0` for a long-running kiosk (an
Ornstein-Uhlenbeck pull in log space, half-life ~17 simulated sessions).
Leave `mean_reversion` at 0.0 otherwise — it is no longer GBM, and session
rollover already solves the change-percentage problem.

Measured by running the code above:

| Property | Target | Measured |
|---|---|---|
| Realised volatility, AAPL over 100k ticks | 0.28 | 0.2804 |
| Correlation AAPL/GOOGL (same sector) | 0.60 | 0.597 |
| Correlation AAPL/JPM (cross sector) | 0.35 | 0.354 |
| Mean absolute per-tick move, AAPL at ~$190 | — | $0.145 |
| Session change over 51 sessions | — | mean −0.11%, stdev 1.45% |
| Prices positive over 20k ticks at `tick_minutes=30` | all | all |
| Derived seed price for PYPL | stable | $117.09 across processes |

A typical simulated session lands within a couple of percent of flat, which
is what a real watchlist looks like.

---

## 8. Massive Client

**File: `backend/app/market/massive.py`**

### 8.1 What the plan tier dictates

From `MASSIVE_API.md` §3, and this table drives every decision below:

| Plan | Rate limit | Recency | Batch endpoint available |
|---|---|---|---|
| Stocks Basic (free) | 5 calls/min | End-of-day only | `/v2/aggs/grouped/...` only |
| Starter $29 / Developer $79 | Unlimited | 15-min delayed | `/v2/snapshot/...` |
| Advanced $199+ | Unlimited | Real-time | `/v2/snapshot/...` |

Two consequences that are easy to miss: **snapshot endpoints 403 on the free
plan**, and **a free key returns end-of-day prices that do not move during
the day**. A free Massive key produces a trading terminal whose prices are
correct and motionless — which is exactly why `PLAN.md` makes the simulator
the default. (`MASSIVE_API.md` §4.3 also suggests free keys see grouped bars
about a day late; that is *inferred* from the recency tier, not verified —
`REVIEW.md` finding 8. Nothing in this design depends on it.)

The provider therefore starts optimistic and downgrades on evidence:

```
fetch() -> snapshot mode -> 403 -> log once, widen poll_interval to 15s -> grouped mode
```

There is no `MASSIVE_PLAN` environment variable to get wrong. The key's tier
is discovered, once, from the API's own answer.

### 8.2 Reading a price out of a snapshot row

In preference order, per `MASSIVE_API.md` §4.1: `lastTrade.p` (the actual
last print, absent unless the plan includes trades), then `min.c` (close of
the most recent minute bar), then `day.c` (close of the running daily bar,
which is **0 before the first trade of the session**). If all three are
empty, fall back to `prevDay.c` so the UI never renders $0.00. `prevDay.c`
is the reference for the daily change.

Timestamps differ by endpoint and this bites: aggregate `t` is
**milliseconds**, snapshot `updated` / `lastTrade.t` are **nanoseconds**.
The two converters at the bottom of the module are the whole fix.

### 8.3 One HTTP call per fetch — the free-tier rule

`REVIEW.md` finding 1: naively walking back five calendar days to find the
last trading session costs up to 4-5 calls in a single fetch, and the free
tier allows 5 per minute. The fix is to spread the search across polls
instead of packing it into one:

- Each fetch asks about exactly **one** date — never a loop.
- An empty result steps the candidate back one day; the *next* poll asks
  about that one. A cold start on a Monday therefore resolves Friday's bar on
  the fourth poll (~45s at a 15s interval) having spent one call each.
- Once resolved, the grouped payload is cached and re-served with **zero**
  HTTP calls until `SESSION_REFRESH_SECONDS` (15 min) elapses or a ticker
  outside the cached set is requested. End-of-day data does not change
  within a session, so re-fetching it is pure waste.
- After 5 fruitless days the search resets and logs a warning, keeping the
  last known prices rather than blanking the board.

Measured call counts across six consecutive fetches on a free key, starting
from a Monday with Friday as the last published session:

```
fetch 1: 2 calls (snapshot 403 + Monday grouped, empty)  -> {}
fetch 2: 1 call  (Sunday, empty)                          -> {}
fetch 3: 1 call  (Saturday, empty)                        -> {}
fetch 4: 1 call  (Friday, 3 rows)                         -> {AAPL, MSFT}
fetch 5: 0 calls (served from the cached session bar)     -> {AAPL, MSFT}
fetch 6: 0 calls                                          -> {AAPL, MSFT}
```

Peak is 2 calls, on the one fetch that discovers the downgrade; steady state
is 0-1. At a 15s poll that is at most 4 calls/min against a 5/min budget.

### 8.4 Code

```python
"""Massive (formerly Polygon.io) REST implementation of MarketDataProvider."""

import asyncio
import logging
import time
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone

import httpx

from .provider import MarketDataProvider
from .types import Quote

logger = logging.getLogger(__name__)

BASE_URL = "https://api.massive.com"
SNAPSHOT_PATH = "/v2/snapshot/locale/us/markets/stocks/tickers"
GROUPED_PATH = "/v2/aggs/grouped/locale/us/market/stocks/{day}"
PREV_PATH = "/v2/aggs/ticker/{ticker}/prev"

FREE_TIER_POLL_SECONDS = 15.0     # 4 calls/min against a 5/min budget
SESSION_REFRESH_SECONDS = 900.0   # how often to look for a newer session bar
MAX_LOOKBACK_DAYS = 5
RETRY_STATUS = frozenset({413, 429, 499, 500, 502, 503, 504})


class MassiveProvider(MarketDataProvider):
    """Fetches prices from Massive, degrading to free-tier endpoints as needed."""

    name = "massive"

    def __init__(
        self,
        api_key: str,
        poll_interval: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ):
        self.poll_interval = poll_interval
        self._mode = "snapshot"
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        self._session_date: date | None = None
        self._candidate: date | None = None
        self._searched = 0
        self._resolved_at = 0.0
        self._grouped: dict[str, Quote] = {}

    # ---------------------------------------------------------------- fetch

    async def fetch(self, tickers: Sequence[str]) -> dict[str, Quote]:
        if not tickers:
            return {}
        if self._mode == "snapshot":
            try:
                return await self._fetch_snapshot(tickers)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 403:
                    raise
                self._downgrade()
        return await self._fetch_grouped(tickers)

    def _downgrade(self) -> None:
        """Switch to free-tier endpoints after a 403 on the snapshot endpoint."""
        self._mode = "grouped"
        self.poll_interval = max(self.poll_interval, FREE_TIER_POLL_SECONDS)
        logger.warning(
            "massive: snapshot endpoint not available on this plan, "
            "falling back to end-of-day grouped bars at %.0fs",
            self.poll_interval,
        )

    async def _fetch_snapshot(self, tickers: Sequence[str]) -> dict[str, Quote]:
        """Current prices via /v2/snapshot. Starter plan and above."""
        response = await self._get(SNAPSHOT_PATH, {"tickers": ",".join(tickers)})
        quotes: dict[str, Quote] = {}
        for row in response.json().get("tickers", []):
            prev_close = _as_float(row.get("prevDay", {}).get("c"))
            price = _snapshot_price(row) or prev_close
            if not price:
                continue
            quotes[row["ticker"]] = Quote(
                ticker=row["ticker"],
                price=price,
                reference=prev_close or price,
                timestamp=_from_nanos(row.get("updated")),
                basis="prev_close",
            )
        return quotes

    async def _fetch_grouped(self, tickers: Sequence[str]) -> dict[str, Quote]:
        """End-of-day closes. At most one HTTP call per fetch, often zero."""
        wanted = set(tickers)
        if self._grouped_is_usable(wanted):
            return {t: self._grouped[t] for t in wanted if t in self._grouped}

        day = self._candidate_day()
        rows = await self._grouped_bars(day)
        if not rows:
            self._step_back(day)
            return {t: self._grouped[t] for t in wanted if t in self._grouped}

        self._session_date = day
        self._candidate = None
        self._searched = 0
        self._resolved_at = time.monotonic()
        self._grouped = {
            row["T"]: Quote(
                ticker=row["T"],
                price=float(row["c"]),
                reference=float(row["o"]),
                timestamp=_from_millis(row.get("t")),
                basis="session_open",
            )
            for row in rows
            if row["T"] in wanted
        }
        logger.info(
            "massive: grouped session %s resolved, %d of %d tickers priced",
            day.isoformat(),
            len(self._grouped),
            len(wanted),
        )
        return dict(self._grouped)

    def _grouped_is_usable(self, wanted: set[str]) -> bool:
        """True when the cached session bar already answers this request."""
        if self._session_date is None or self._candidate is not None:
            return False
        if time.monotonic() - self._resolved_at > SESSION_REFRESH_SECONDS:
            return False
        return wanted <= self._grouped.keys()

    def _candidate_day(self) -> date:
        """The single date this fetch will ask about."""
        if self._candidate is not None:
            return self._candidate
        self._candidate = datetime.now(timezone.utc).date()
        return self._candidate

    def _step_back(self, day: date) -> None:
        """Walk one calendar day back — one poll, one day, one call."""
        self._searched += 1
        if self._searched >= MAX_LOOKBACK_DAYS:
            logger.warning(
                "massive: no grouped bar in the %d days ending %s; "
                "keeping last known prices",
                MAX_LOOKBACK_DAYS,
                day.isoformat(),
            )
            self._candidate = None
            self._searched = 0
            self._resolved_at = time.monotonic()
            return
        self._candidate = day - timedelta(days=1)

    async def _grouped_bars(self, day: date) -> list[dict]:
        """Grouped daily bars for one date. Empty list on a non-trading day."""
        response = await self._get(
            GROUPED_PATH.format(day=day.isoformat()), {"adjusted": "true"}
        )
        return response.json().get("results") or []

    # ----------------------------------------------------------- validation

    async def validate_ticker(self, ticker: str) -> bool:
        """Check a symbol exists before it joins the watchlist. Free on all plans."""
        try:
            response = await self._get(PREV_PATH.format(ticker=ticker), {})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (403, 404):
                return False
            raise
        return bool(response.json().get("resultsCount"))

    # ------------------------------------------------------------- plumbing

    async def _get(self, path: str, params: dict, attempts: int = 4) -> httpx.Response:
        """GET with exponential backoff on transient statuses. Never retries 401/403."""
        for attempt in range(attempts):
            response = await self._client.get(path, params=params)
            if response.status_code not in RETRY_STATUS:
                response.raise_for_status()
                return response
            if attempt < attempts - 1:
                await asyncio.sleep(0.2 * 2**attempt)
        response.raise_for_status()
        return response

    async def aclose(self) -> None:
        await self._client.aclose()


def _snapshot_price(row: dict) -> float:
    """Best available current price from a v2 snapshot row, most reliable first."""
    for value in (
        row.get("lastTrade", {}).get("p"),
        row.get("min", {}).get("c"),
        row.get("day", {}).get("c"),
    ):
        if value:
            return float(value)
    return 0.0


def _as_float(value) -> float:
    return float(value) if value else 0.0


def _from_nanos(value) -> datetime:
    """Snapshot timestamps are nanoseconds since epoch."""
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(value / 1e9, timezone.utc)


def _from_millis(value) -> datetime:
    """Aggregate bar timestamps are milliseconds since epoch."""
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(value / 1e3, timezone.utc)
```

Points worth calling out:

- **One HTTP call per fetch in both modes.** Snapshot takes a `tickers=`
  filter; grouped returns the whole market and is filtered client-side.
  Watchlist size never changes the call count.
- **`httpx`, not the official `massive` client.** The official client is
  synchronous urllib3 and would block the event loop; this provider needs
  exactly three endpoints. `uv add httpx`, and `uv remove massive numpy` —
  neither is needed any more.
- **The `client` constructor argument exists for tests.** Passing an
  `httpx.AsyncClient` built on `httpx.MockTransport` gives full-fidelity
  request/response testing with no network and no extra dependency (§16.4).
- **Grouped mode reports `basis="session_open"`.** Both values come from the
  same bar, so the change is that session's move, and the frontend labels the
  column accordingly instead of lying about "vs prev close"
  (`REVIEW.md` finding 4).

### 8.5 Checking a key by hand

```bash
# Snapshot: 200 on Starter+, 403 on the free plan
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $MASSIVE_API_KEY" \
  "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT"

# Grouped daily bars: works on every plan
curl -H "Authorization: Bearer $MASSIVE_API_KEY" \
  "https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/2026-08-26?adjusted=true"

# Is the market open right now?
curl -H "Authorization: Bearer $MASSIVE_API_KEY" \
  "https://api.massive.com/v1/marketstatus/now"
```

A 401 (`{"status":"ERROR","error":"Unknown API Key"}`) means the key is bad —
fail fast at startup rather than retrying; the answer will not change.

---

## 9. Price Cache

**File: `backend/app/market/cache.py`**

```python
"""In-memory store of the latest tick per ticker."""

from collections.abc import Iterable

from .types import Direction, PriceTick, Quote


class PriceCache:
    """Latest known price per ticker, plus tick-over-tick direction.

    Single-writer (the engine), many-reader (SSE, routes). `version` is a
    monotonic counter that increments only when something actually changed,
    so an SSE client can cheaply tell whether it has anything new to send.
    """

    def __init__(self) -> None:
        self._ticks: dict[str, PriceTick] = {}
        self._version = 0

    @property
    def version(self) -> int:
        return self._version

    def update(self, quotes: Iterable[Quote]) -> list[PriceTick]:
        """Apply quotes and return only the ticks whose price actually moved."""
        changed: list[PriceTick] = []
        for quote in quotes:
            existing = self._ticks.get(quote.ticker)
            if (
                existing is not None
                and existing.price == quote.price
                and existing.reference == quote.reference
            ):
                continue  # a repeated end-of-day price is not news
            previous_price = existing.price if existing else quote.price
            tick = PriceTick(
                ticker=quote.ticker,
                price=quote.price,
                previous_price=previous_price,
                reference=quote.reference,
                timestamp=quote.timestamp,
                basis=quote.basis,
                direction=_direction(previous_price, quote.price),
            )
            self._ticks[quote.ticker] = tick
            changed.append(tick)
        if changed:
            self._version += 1
        return changed

    def get(self, ticker: str) -> PriceTick | None:
        """Latest tick for one ticker, or None if never seen."""
        return self._ticks.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        """Convenience for trade execution and portfolio valuation."""
        tick = self._ticks.get(ticker)
        return tick.price if tick else None

    def snapshot(self) -> dict[str, PriceTick]:
        """A copy of every known tick, safe to iterate."""
        return dict(self._ticks)

    def prune(self, keep: Iterable[str]) -> int:
        """Drop tickers nobody is watching or holding. Returns how many went."""
        keeping = set(keep)
        dropped = [t for t in self._ticks if t not in keeping]
        for ticker in dropped:
            del self._ticks[ticker]
        if dropped:
            self._version += 1
        return len(dropped)

    def __len__(self) -> int:
        return len(self._ticks)

    def __contains__(self, ticker: str) -> bool:
        return ticker in self._ticks


def _direction(previous: float, current: float) -> Direction:
    if current > previous:
        return "up"
    if current < previous:
        return "down"
    return "flat"
```

### 9.1 Notes

- **The first observation sets `previous_price == price`**, so `direction` is
  `"flat"` and the UI does not flash a fake move on page load.
- **No lock.** Everything runs in one asyncio event loop, the engine is the
  only writer, and neither `dict` assignment nor `dict(...)` yields control.
  A lock here would protect against nothing. (If a future provider ever needs
  a worker thread, it should hand quotes back to the loop rather than write
  the cache from the thread.)
- **`prune` is wired**, not decorative — the engine calls it every poll
  (`REVIEW.md` finding 5). Its input is the union of watchlist and position
  tickers (§14), never the watchlist alone, or selling a position after
  removing it from the watchlist would find no price.
- **Version bumps only on real change**, which keeps a motionless free-tier
  feed from re-broadcasting the same payload every 15 seconds.

Verified behaviour:

```
first quote  -> direction "flat", version 1
higher quote -> direction "up", previous_price 190.0, version 2
same quote   -> update() returns [], version stays 2
```

---

## 10. Price Engine

**File: `backend/app/market/engine.py`**

```python
"""Background task that keeps the price cache current."""

import asyncio
import logging
from collections.abc import Callable, Sequence

from .cache import PriceCache
from .provider import MarketDataProvider

logger = logging.getLogger(__name__)


class PriceEngine:
    """Polls a provider on its advised interval and writes into the cache."""

    def __init__(
        self,
        provider: MarketDataProvider,
        cache: PriceCache,
        tickers: Callable[[], Sequence[str]],
    ):
        self._provider = provider
        self._cache = cache
        self._tickers = tickers
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Begin polling. Idempotent."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="price-engine")

    async def stop(self) -> None:
        """Cancel the poll loop and release the provider."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._provider.aclose()

    async def poll_once(self) -> None:
        """One fetch-and-store cycle. Used by the loop and directly by tests."""
        tickers = sorted({t.upper() for t in self._tickers()})
        if not tickers:
            return
        quotes = await self._provider.fetch(tickers)
        self._cache.update(quotes.values())
        self._cache.prune(tickers)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("price engine poll failed, keeping last prices")
            # Anchor the next wake-up to the schedule, not to when this poll
            # finished, so a slow fetch does not stretch the cadence.
            deadline = max(deadline + self._provider.poll_interval, loop.time())
            await asyncio.sleep(deadline - loop.time())
```

- **The `tickers` callable** is what makes watchlist changes take effect
  without a restart. In the app it reads SQLite (§14); in tests it is a
  lambda over a list.
- **The broad `except Exception`** is one of the few places defensive code
  earns its place. A transient upstream failure must not kill the only task
  feeding every connected SSE client: the cache keeps its last values, prices
  go stale rather than blank, and the next poll recovers on its own.
- **`provider.poll_interval` is read every iteration**, so the Massive
  downgrade from 5s to 15s takes effect immediately.
- **Deadline-based sleep** (`REVIEW.md` finding 6). Measured: six polls at
  `poll_interval=0.1` with a 0.05s fetch take **0.62s**, not the 0.9s that
  `sleep(interval)` after the fetch would give.

---

## 11. Provider Selection

**File: `backend/app/market/factory.py`**

```python
"""Provider selection driven by environment variables."""

import logging
import os

from .massive import MassiveProvider
from .provider import MarketDataProvider
from .simulator import SimulatorProvider

logger = logging.getLogger(__name__)


def create_provider() -> MarketDataProvider:
    """Return the Massive provider if an API key is configured, else the simulator."""
    api_key = os.getenv("MASSIVE_API_KEY", "").strip()
    override = os.getenv("MARKET_POLL_SECONDS", "").strip()

    provider: MarketDataProvider
    if api_key:
        provider = MassiveProvider(api_key=api_key)
    else:
        provider = SimulatorProvider()

    if override:
        provider.poll_interval = float(override)

    logger.info(
        "market: provider=%s poll_interval=%.2fs",
        provider.name,
        provider.poll_interval,
    )
    return provider
```

`.strip()` matters: `MASSIVE_API_KEY=` in a `.env` file yields an empty
string, not an unset variable, and `PLAN.md` §5 specifies that
absent-or-empty selects the simulator.

Log the choice once at startup — "which data source am I looking at" is the
first question anyone asks when prices look wrong:

```
market: provider=simulator poll_interval=0.50s
market: provider=massive poll_interval=5.00s
massive: snapshot endpoint not available on this plan, falling back to end-of-day grouped bars at 15s
```

---

## 12. SSE Streaming

**File: `backend/app/market/stream.py`**

```python
"""SSE endpoint that pushes cached prices to the browser."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)

PUSH_INTERVAL = 0.5
HEARTBEAT_SECONDS = 15.0


def create_stream_router(cache: PriceCache) -> APIRouter:
    """Build the /api/stream router bound to a price cache."""
    router = APIRouter(prefix="/api/stream", tags=["stream"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """Live price feed. Consume with the browser's native EventSource."""
        return StreamingResponse(
            _events(cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # defeat proxy buffering
            },
        )

    return router


async def _events(cache: PriceCache, request: Request) -> AsyncIterator[str]:
    """Yield an SSE frame whenever the cache changes, plus periodic heartbeats."""
    yield "retry: 1000\n\n"
    last_version = -1
    last_send = 0.0
    loop = asyncio.get_running_loop()
    try:
        while not await request.is_disconnected():
            now = loop.time()
            if cache.version != last_version:
                last_version = cache.version
                last_send = now
                yield _frame(cache)
            elif now - last_send >= HEARTBEAT_SECONDS:
                last_send = now
                yield ": heartbeat\n\n"
            await asyncio.sleep(PUSH_INTERVAL)
    except asyncio.CancelledError:
        raise
    finally:
        logger.info("sse: client disconnected")


def _frame(cache: PriceCache) -> str:
    """One `prices` event carrying every known tick."""
    payload = {
        "version": cache.version,
        "prices": [tick.to_dict() for tick in cache.snapshot().values()],
    }
    return f"event: prices\ndata: {json.dumps(payload)}\n\n"
```

### 12.1 Wire format

The first frame is a retry directive; then one `prices` event per change,
carrying every known ticker (ten tickers is ~1.2KB — batching beats
per-ticker events, which would multiply framing overhead for no benefit):

```
retry: 1000

event: prices
data: {"version": 6, "prices": [
  {"ticker": "AAPL", "price": 190.08, "previous_price": 189.94, "reference": 190.0,
   "basis": "prev_close", "direction": "up", "change": 0.08, "change_percent": 0.04,
   "timestamp": "2026-08-27T08:59:40.464100+00:00"},
  {"ticker": "MSFT", "price": 419.9, "previous_price": 420.03, "reference": 420.0,
   "basis": "prev_close", "direction": "down", "change": -0.1, "change_percent": -0.02,
   "timestamp": "2026-08-27T08:59:40.464100+00:00"}]}

: heartbeat
```

(Captured from `curl -N http://localhost:8000/api/stream/prices`, reformatted
for reading — real frames are single-line.)

Design points:

- **Push on change, poll to detect it.** The generator wakes every 500ms and
  compares `cache.version`. Cheap, no pub/sub machinery, and a motionless
  free-tier feed sends nothing but heartbeats.
- **Heartbeat comment lines** (`: heartbeat`) every 15s keep proxies and load
  balancers from reaping an idle connection. `EventSource` ignores them.
- **`retry: 1000`** tells the browser to reconnect after a second;
  `EventSource` handles reconnection itself, which is the whole reason
  `PLAN.md` chose SSE over WebSockets.
- **Disconnect detection** via `request.is_disconnected()`, so a closed tab
  ends the generator instead of leaking a task per page load.

### 12.2 Frontend consumption

```ts
const source = new EventSource("/api/stream/prices");

source.addEventListener("prices", (event) => {
  const { prices } = JSON.parse(event.data);
  for (const tick of prices) {
    applyTick(tick);          // update the row, flash on tick.direction
    pushSparkline(tick.ticker, tick.price);
  }
});

source.onerror = () => setConnectionStatus("reconnecting");  // EventSource retries
source.onopen = () => setConnectionStatus("connected");
```

`direction` drives the flash class; `change_percent` plus `basis` drive the
daily-change column ("vs prev close" or "since open"); accumulating `price`
per ticker builds the sparklines `PLAN.md` §2 describes.

---

## 13. Application Wiring

**File: `backend/app/main.py`** (market data portions only)

```python
"""FastAPI application factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from app.market import PriceCache, PriceEngine, create_provider, create_stream_router
from app.db import watched_tickers  # SQLite: watchlist ∪ position tickers


def create_app() -> FastAPI:
    cache = PriceCache()
    provider = create_provider()
    engine = PriceEngine(provider, cache, tickers=watched_tickers)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.prices = cache
        app.state.provider = provider
        engine.start()
        try:
            yield
        finally:
            await engine.stop()

    app = FastAPI(title="FinAlly", lifespan=lifespan)
    app.include_router(create_stream_router(cache))
    return app


app = create_app()
```

Build the cache and router **before** `FastAPI(...)` rather than inside
`lifespan`. Including a router from a startup hook is a trap: when a
`lifespan` is supplied, FastAPI ignores `@app.on_event("startup")` entirely,
and the route silently 404s. (Confirmed the hard way; the wiring above was
run under uvicorn and curled.)

Reading a price anywhere else in the backend:

```python
def current_price(request: Request, ticker: str) -> float:
    """The price a trade fills at, or a 422 if the ticker has no price yet."""
    tick = request.app.state.prices.get(ticker.upper())
    if tick is None:
        raise HTTPException(422, f"No price available for {ticker.upper()}")
    return tick.price
```

Every consumer — trade execution, `/api/portfolio` valuation, the 30-second
portfolio snapshot task, the LLM's context builder — goes through the cache.
That is what guarantees a trade fills at exactly the price on screen.

---

## 14. Watchlist Coordination

### 14.1 What the engine polls

```python
def watched_tickers() -> list[str]:
    """Every ticker the app needs a price for: watchlist plus open positions."""
    with connect() as db:
        rows = db.execute(
            "SELECT ticker FROM watchlist WHERE user_id = ?"
            " UNION "
            "SELECT ticker FROM positions WHERE user_id = ? AND quantity > 0",
            ("default", "default"),
        ).fetchall()
    return [row[0] for row in rows]
```

The union is essential. A user can remove a ticker from the watchlist while
still holding it; without the positions half, the price stops updating and
the portfolio silently values that holding at a stale price — or, after
`prune`, at none at all.

### 14.2 Adding a ticker

```python
@router.post("/api/watchlist")
async def add_ticker(body: TickerIn, request: Request):
    """Validate, then persist. The engine picks it up on its next poll."""
    symbol = normalize_symbol(body.ticker)
    if symbol is None:
        raise HTTPException(422, f"{body.ticker!r} is not a valid symbol")
    if not await request.app.state.provider.validate_ticker(symbol):
        raise HTTPException(404, f"{symbol} is not a known ticker")
    insert_watchlist(symbol)          # INSERT OR IGNORE — the UNIQUE constraint
    return {"ticker": symbol}
```

This is the resolution of `REVIEW.md` finding 2. Existence is checked once,
here, where an HTTP round trip and a real error message are both affordable —
not inside the poll loop, and not by pattern-matching on an absent key in a
`fetch()` result. Under the simulator every syntactically valid symbol is
accepted and priced from its hashed spec; under Massive an unknown symbol is
rejected with a 404 the user can act on. Both are coherent; neither depends
on the poll loop's behaviour.

The new ticker appears in the cache within one poll interval (0.5s simulated,
up to 15s on a free key). Until then, the row renders as "—" rather than
$0.00, and the trade route returns 422 rather than filling at a made-up
price.

### 14.3 Removing a ticker

```python
@router.delete("/api/watchlist/{ticker}")
async def remove_ticker(ticker: str):
    """Remove from the watchlist. Prices keep flowing if a position remains."""
    delete_watchlist(ticker.upper())
    return {"ticker": ticker.upper(), "removed": True}
```

No cache manipulation here. The engine's next `prune(tickers)` drops the
ticker if — and only if — nothing else needs it, which is exactly the
condition `watched_tickers()` already expresses.

---

## 15. Error Handling and Edge Cases

| Situation | Behaviour | Where |
|---|---|---|
| Empty watchlist at startup | `poll_once` returns early; no provider call | `engine.poll_once` |
| Ticker requested before its first tick | `get()` returns None → route raises 422 | `main.current_price` |
| Provider raises mid-poll | Logged with traceback; cache keeps last values; next poll retries | `engine._run` |
| Massive 403 on snapshot | One warning, mode → grouped, `poll_interval` → 15s, never retried | `massive._downgrade` |
| Massive 401 (bad key) | `raise_for_status` propagates; logged each poll — check the key | `massive._get` |
| Massive 429 / 5xx | Up to 4 attempts with 0.2/0.4/0.8s backoff | `massive._get` |
| Non-trading date (200, `results: []`) | Not an error: candidate steps back one day, one call per poll | `massive._step_back` |
| No session bar in 5 days | Warning; search resets; last known prices retained | `massive._step_back` |
| `day.c == 0` pre-market | Falls through `lastTrade.p` → `min.c` → `day.c` → `prevDay.c` | `massive._snapshot_price` |
| Ticker missing from snapshot response | Omitted from the result; its cached tick simply stops updating | `massive._fetch_snapshot` |
| Free-tier price never changes | Cache suppresses no-op updates; SSE sends heartbeats only | `cache.update` |
| Simulator asked for an unknown symbol | Priced from a SHA-256-derived spec, stable across restarts | `simulator.derive_spec` |
| Simulated price approaching zero | Floored at $0.01; GBM cannot reach it anyway | `simulator._step` |
| SSE client disappears | `is_disconnected()` ends the generator; no leaked tasks | `stream._events` |
| Shutdown | Engine task cancelled, `aclose()` closes the HTTP client | `engine.stop` |

Rounding: prices are rounded to 2dp at the provider boundary (simulator) or
taken as given (Massive), so the cache, the trade fill, and the number on
screen are the same float. Portfolio math never re-rounds a price it did not
fill at.

---

## 16. Testing Strategy

### 16.1 Dependencies

`REVIEW.md` finding 7. Everything below runs on `pytest` + `pytest-asyncio`
with `asyncio_mode = "auto"` (already set in `backend/pyproject.toml`), plus
`httpx`, which the Massive provider needs anyway. HTTP mocking uses
`httpx.MockTransport` from httpx itself — no `pytest-httpx`, no
`responses`, no network. `statistics.correlation` needs Python 3.10+;
the project is on 3.12.

```
backend/tests/market/
├── test_types.py         # change/change_percent, to_dict wire shape
├── test_contract.py      # parametrized over every provider
├── test_simulator.py     # GBM statistics, determinism, rollover
├── test_massive.py       # MockTransport: parsing, downgrade, call counts, retries
├── test_cache.py         # direction, no-op suppression, prune, version
├── test_engine.py        # poll_once, prune wiring, cadence, error resilience
└── test_stream.py        # SSE frame format
```

### 16.2 Contract suite

Parametrize one suite over every implementation so a new provider inherits
the whole thing:

```python
import httpx
import pytest

from app.market import MassiveProvider, SimulatorProvider

TICKERS = ["AAPL", "GOOGL", "MSFT"]


def _snapshot_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "OK", "tickers": [
            {"ticker": t, "day": {"c": 100.0}, "prevDay": {"c": 99.0}}
            for t in TICKERS
        ]})
    return httpx.MockTransport(handler)


@pytest.fixture(params=["simulator", "massive"])
def provider(request):
    if request.param == "simulator":
        return SimulatorProvider(seed=42)
    client = httpx.AsyncClient(base_url="https://api.massive.com",
                               transport=_snapshot_transport())
    return MassiveProvider(api_key="test-key", client=client)


async def test_keys_are_a_subset_of_the_request(provider):
    quotes = await provider.fetch(TICKERS)
    assert set(quotes) <= set(TICKERS)
    assert all(quotes[t].ticker == t for t in quotes)


async def test_prices_and_references_are_positive(provider):
    quotes = await provider.fetch(TICKERS)
    assert all(q.price > 0 and q.reference > 0 for q in quotes.values())


async def test_empty_request_does_no_io(provider):
    assert await provider.fetch([]) == {}


async def test_quotes_are_timezone_aware(provider):
    quotes = await provider.fetch(TICKERS)
    assert all(q.timestamp.tzinfo is not None for q in quotes.values())


async def test_aclose_is_idempotent(provider):
    await provider.aclose()
    await provider.aclose()
```

Note what is *not* asserted: that an unknown ticker is omitted. The simulator
prices everything by design, so that belongs in provider-specific tests, with
`validate_ticker` carrying the shared meaning instead.

For route and E2E tests, a third implementation keeps portfolio values exact:

```python
class FixedProvider(MarketDataProvider):
    """Returns preset prices. For tests that need a known portfolio value."""

    name = "fixed"
    poll_interval = 0.05

    def __init__(self, prices: dict[str, float]):
        self._prices = prices

    async def fetch(self, tickers):
        now = datetime.now(timezone.utc)
        return {
            t: Quote(t, self._prices[t], self._prices[t], now)
            for t in tickers
            if t in self._prices
        }
```

### 16.3 Simulator tests

Statistical properties, not exact values. The 100k-tick volatility test runs
in 0.63s; keep it — it is what catches a dropped `- sigma^2/2` term or a `dt`
that is off by a trading-calendar factor. Every test in this section was run
as written: 23 tests, 3.8 seconds, all passing.

```python
import math
import statistics

import pytest

from app.market.simulator import TRADING_MINUTES_PER_YEAR, SimulatorProvider

TICKERS = ["AAPL", "GOOGL", "JPM"]


async def _paths(provider, tickers, steps):
    series = {t: [] for t in tickers}
    for _ in range(steps):
        for ticker, quote in (await provider.fetch(tickers)).items():
            series[ticker].append(quote.price)
    return series


async def test_recovers_configured_volatility():
    provider = SimulatorProvider(seed=1, event_probability=0.0)
    prices = (await _paths(provider, ["AAPL"], 100_000))["AAPL"]
    returns = [math.log(b / a) for a, b in zip(prices, prices[1:])]
    realized = statistics.stdev(returns) / math.sqrt(1 / TRADING_MINUTES_PER_YEAR)
    assert realized == pytest.approx(0.28, rel=0.05)      # measured 0.2804


async def test_same_sector_pair_is_more_correlated():
    provider = SimulatorProvider(seed=2, event_probability=0.0)
    series = await _paths(provider, TICKERS, 50_000)
    rets = {t: [math.log(b / a) for a, b in zip(p, p[1:])] for t, p in series.items()}
    assert statistics.correlation(rets["AAPL"], rets["GOOGL"]) == pytest.approx(0.60, abs=0.05)
    assert statistics.correlation(rets["AAPL"], rets["JPM"]) == pytest.approx(0.35, abs=0.05)


async def test_prices_stay_positive_under_an_aggressive_clock():
    series = await _paths(SimulatorProvider(seed=3, tick_minutes=30.0), TICKERS, 20_000)
    assert all(price > 0 for prices in series.values() for price in prices)


async def test_paths_are_deterministic_and_order_independent():
    a = await _paths(SimulatorProvider(seed=7), ["AAPL", "JPM"], 200)
    b = await _paths(SimulatorProvider(seed=7), ["JPM", "AAPL"], 200)
    c = await _paths(SimulatorProvider(seed=8), ["AAPL", "JPM"], 200)
    assert a == b
    assert a != c


async def test_unknown_ticker_gets_a_stable_seed_price():
    first = await SimulatorProvider(seed=1).fetch(["PYPL"])
    second = await SimulatorProvider(seed=99).fetch(["PYPL"])
    assert first["PYPL"].reference == second["PYPL"].reference == 117.09


async def test_session_rollover_resets_the_reference():
    """After 390 ticks the reference tracks the price, not the seed price."""
    provider = SimulatorProvider(seed=4, event_probability=0.0)
    for _ in range(390):
        quotes = await provider.fetch(["AAPL"])
    quote = quotes["AAPL"]
    assert quote.reference != 190.00                              # moved off the seed
    assert quote.reference == pytest.approx(quote.price, abs=1.0)  # and tracks the price
```

That last test is `REVIEW.md` finding 3. The old `abs=0.01` tolerance passed
for one lucky seed and failed for 54 of 60 others, because `_advance` sets
the close from the price *before* stepping — so after the rollover tick the
price is already one step away. The assertion above states the property that
actually matters and holds across seeds.

### 16.4 Massive tests

```python
from datetime import date

import httpx
import pytest

from app.market import MassiveProvider


def _provider(handler) -> MassiveProvider:
    client = httpx.AsyncClient(base_url="https://api.massive.com",
                               transport=httpx.MockTransport(handler))
    return MassiveProvider(api_key="test-key", client=client)


async def test_snapshot_prefers_last_trade_and_falls_back_to_prev_close():
    def handler(request):
        return httpx.Response(200, json={"tickers": [
            {"ticker": "AAPL", "updated": 1605192894630916600, "lastTrade": {"p": 190.55},
             "min": {"c": 190.4}, "day": {"c": 190.5}, "prevDay": {"c": 188.0}},
            {"ticker": "GOOGL", "day": {"c": 0}, "min": {"c": 0}, "prevDay": {"c": 175.0}},
        ]})
    quotes = await _provider(handler).fetch(["AAPL", "GOOGL", "NOPE"])
    assert quotes["AAPL"].price == 190.55                 # lastTrade wins
    assert quotes["AAPL"].change_percent == pytest.approx(1.36, abs=0.01)
    assert quotes["GOOGL"].price == 175.0                 # zero day.c -> prevDay.c
    assert "NOPE" not in quotes                           # absent, not raised


async def test_free_key_downgrades_once_and_spends_one_call_per_fetch():
    calls: list[str] = []
    published = date(2026, 8, 21)   # a Friday

    def handler(request):
        calls.append(request.url.path)
        if "snapshot" in request.url.path:
            return httpx.Response(403, json={"error": "NOT_AUTHORIZED"})
        day = request.url.path.rsplit("/", 1)[-1]
        if day == published.isoformat():
            return httpx.Response(200, json={"results": [
                {"T": "AAPL", "o": 188.0, "c": 190.1, "t": 1755792000000},
                {"T": "MSFT", "o": 420.0, "c": 418.2, "t": 1755792000000},
            ]})
        return httpx.Response(200, json={"resultsCount": 0, "results": []})

    provider = _provider(handler)
    provider._candidate = date(2026, 8, 24)   # pretend today is the Monday
    per_fetch = []
    for _ in range(6):
        before = len(calls)
        quotes = await provider.fetch(["AAPL", "MSFT"])
        per_fetch.append((len(calls) - before, sorted(quotes)))

    assert provider.poll_interval == 15.0
    assert [n for n, _ in per_fetch] == [2, 1, 1, 1, 0, 0]      # never a 5-call fetch
    assert per_fetch[-1][1] == ["AAPL", "MSFT"]
    assert provider._session_date == published
    quotes = await provider.fetch(["AAPL", "MSFT"])
    assert quotes["AAPL"].basis == "session_open"


async def test_validate_ticker_distinguishes_real_symbols():
    def handler(request):
        if "NOTATICKER" in request.url.path:
            return httpx.Response(404, json={"status": "ERROR"})
        return httpx.Response(200, json={"resultsCount": 1})
    provider = _provider(handler)
    assert await provider.validate_ticker("AAPL") is True
    assert await provider.validate_ticker("NOTATICKER") is False


async def test_retries_transient_statuses():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, json={"status": "ERROR"})
        return httpx.Response(200, json={"tickers": [
            {"ticker": "AAPL", "day": {"c": 1.0}, "prevDay": {"c": 1.0}}]})

    assert "AAPL" in await _provider(handler).fetch(["AAPL"])
    assert attempts["n"] == 3
```

### 16.5 Cache, engine and stream tests

These excerpts assume the module-level imports of the surrounding test file
(`asyncio`, `json`, `datetime`, `httpx`, and the `app.market` names).

```python
async def test_engine_prunes_tickers_that_left_the_watchlist():
    cache = PriceCache()
    watched = ["AAPL", "MSFT"]
    engine = PriceEngine(SimulatorProvider(seed=5), cache, lambda: watched)
    await engine.poll_once()
    assert sorted(cache.snapshot()) == ["AAPL", "MSFT"]
    watched.remove("MSFT")
    await engine.poll_once()
    assert sorted(cache.snapshot()) == ["AAPL"]


async def test_engine_survives_a_failing_provider():
    class Broken(SimulatorProvider):
        async def fetch(self, tickers):
            raise httpx.ConnectError("upstream down")

    cache = PriceCache()
    cache.update([Quote("AAPL", 190.0, 188.0, datetime.now(timezone.utc))])
    engine = PriceEngine(Broken(seed=1, poll_interval=0.01), cache, lambda: ["AAPL"])
    engine.start()
    await asyncio.sleep(0.05)
    await engine.stop()
    assert cache.get_price("AAPL") == 190.0      # stale, not blank
```

Testing the SSE endpoint needs one piece of local knowledge: **httpx's
`ASGITransport` runs the app to completion before returning the response, so
it deadlocks on an endless stream.** Drive the generator directly instead —
it takes anything with an async `is_disconnected()`:

```python
class StubRequest:
    """Stands in for starlette's Request: disconnects after `alive` checks."""

    def __init__(self, alive: int):
        self.alive = alive

    async def is_disconnected(self) -> bool:
        self.alive -= 1
        return self.alive < 0


async def test_stream_emits_retry_then_a_frame_per_change():
    cache = PriceCache()
    now = datetime.now(timezone.utc)
    cache.update([Quote("AAPL", 190.0, 188.0, now)])

    frames = []
    async def drive():
        async for frame in _events(cache, StubRequest(3)):
            frames.append(frame)

    task = asyncio.create_task(drive())
    await asyncio.sleep(0.3)
    cache.update([Quote("AAPL", 191.0, 188.0, now)])
    await task

    assert frames[0] == "retry: 1000\n\n"
    assert all(f.startswith("event: prices\ndata: ") for f in frames[1:])
    payload = json.loads(frames[2].split("data: ", 1)[1])
    assert payload["prices"][0]["direction"] == "up"
    assert payload["prices"][0]["price"] == 191.0
```

E2E (`test/`, Playwright, per `PLAN.md` §12) covers the real transport: load
the page, assert prices change within a few seconds, kill the connection and
assert the status dot returns to green after `EventSource` reconnects.

### 16.6 Manual smoke test

```bash
cd backend
uv run uvicorn app.main:app --port 8000            # simulator by default
curl -N http://localhost:8000/api/stream/prices | head -20
```

---

## 17. Configuration Summary

| Variable | Default | Effect |
|---|---|---|
| `MASSIVE_API_KEY` | unset | Non-empty selects `MassiveProvider`; absent or empty selects the simulator |
| `MARKET_POLL_SECONDS` | provider default | Overrides `poll_interval` at construction (the Massive downgrade may still widen it) |
| `SIMULATOR_SEED` | unset | Fixes the simulator's RNG. Set in E2E; leave unset in production |

Constants worth knowing, all in code rather than environment:

| Constant | Value | Module |
|---|---|---|
| `PUSH_INTERVAL` | 0.5s | `stream.py` — SSE change-detection cadence |
| `HEARTBEAT_SECONDS` | 15s | `stream.py` — keep-alive comment |
| `FREE_TIER_POLL_SECONDS` | 15s | `massive.py` — 4 calls/min against a 5/min limit |
| `SESSION_REFRESH_SECONDS` | 900s | `massive.py` — how often to look for a newer session bar |
| `MAX_LOOKBACK_DAYS` | 5 | `massive.py` — gives up rather than searching forever |
| `TRADING_MINUTES_PER_YEAR` | 98,280 | `simulator.py` — 252 × 390 |
| `SESSION_TICKS` | 390 | `simulator.py` — ticks per simulated session |

Dependencies: `uv add httpx`. `numpy` and `massive` are no longer needed
(`uv remove numpy massive`) — the two-factor correlation model replaces
numpy's Cholesky decomposition, and `httpx` replaces the synchronous
official client. Test dependencies are unchanged: `pytest`,
`pytest-asyncio`, `pytest-cov`, `ruff`.

---

## 18. Migration from the Existing Implementation

`backend/app/market/` currently holds the push-based implementation described
in `MARKET_DATA_SUMMARY.md`. Replace it module for module:

| Existing | Replacement | Why |
|---|---|---|
| `models.PriceUpdate` | `types.Quote` + `types.PriceTick` | Splits what a provider reports from what the cache derives; adds `reference`/`basis` so the UI can show a daily change |
| `interface.MarketDataSource` (owns a background task, writes the cache) | `provider.MarketDataProvider` (pure `fetch`) + `engine.PriceEngine` | Timing leaves the providers; both become plain async functions to test |
| `cache.PriceCache.update(ticker, price)` | `cache.PriceCache.update(quotes)` | Batch write, returns changed ticks, suppresses no-ops, drops the unnecessary lock |
| `simulator.GBMSimulator` + `SimulatorDataSource` (numpy Cholesky) | `simulator.SimulatorProvider` (two-factor, stdlib only) | Same correlations, no numpy, ~90 fewer lines |
| `seed_prices.py` | `simulator.UNIVERSE` / `derive_spec` | One file; adds stable pricing for user-added tickers |
| `massive_client.MassiveDataSource` (sync `massive` package) | `massive.MassiveProvider` (async httpx) | No event-loop blocking; adds the free-tier 403 downgrade and rate-limit discipline |
| `factory.create_market_data_source(cache)` | `factory.create_provider()` | Providers no longer know the cache exists |
| `stream.create_stream_router(cache)` | same name, new payload | Named `prices` event, list payload, `change_percent`, `basis`, heartbeats |
| `tests/market/*` | rewritten per §16 | Contract suite replaces per-implementation duplication |

`market_data_demo.py` needs a small edit: build a `PriceCache` +
`PriceEngine` instead of calling `source.start(tickers)`, and read
`tick.change_percent` for the daily column.

Nothing outside `app/market/` imports these names today, so the blast radius
is the package plus its tests.

---

## 19. Implementation Checklist

1. `uv add httpx`; `uv remove numpy massive`.
2. `types.py`, `provider.py` — no dependencies, no logic to get wrong.
3. `cache.py` + `test_cache.py` — direction, no-op suppression, prune, version.
4. `simulator.py` + `test_simulator.py` — get the volatility and correlation
   tests passing before anything else; they catch the two classic GBM errors.
5. `engine.py` + `test_engine.py` — `poll_once`, prune wiring, cadence,
   survival of a failing provider.
6. `factory.py` — selection and the startup log line.
7. `stream.py` + `test_stream.py` — frame format via the stub request.
8. Wire `create_app()` (§13); `curl -N /api/stream/prices` and watch prices
   move under the simulator. This is the first end-to-end checkpoint.
9. `massive.py` + `test_massive.py` — parsing, 403 downgrade, call-count
   discipline, retries, `validate_ticker`.
10. Watchlist routes (§14) once the DB layer exists; `watched_tickers()` is
    the contract between it and the engine.
11. Optional, with a real key: `MASSIVE_API_KEY=... uv run uvicorn app.main:app`
    and confirm the startup log names the provider and, on a free key, the
    downgrade warning.

Steps 2-8 have no external dependencies and no API key, so the entire live
price feed can be built and demonstrated before the Massive client exists.
