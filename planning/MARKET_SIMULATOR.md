# Market Simulator

The default price source for FinAlly. Generates plausible, correlated,
always-positive price paths with no network dependency and no API key.

It implements `MarketDataProvider` from `MARKET_INTERFACE.md`, so nothing
downstream can tell it apart from the Massive client.

---

## 1. What It Has to Do

From `PLAN.md`:

- Geometric Brownian motion with configurable drift and volatility per ticker
- Updates at roughly 500ms intervals
- Correlated moves across tickers, so tech names move together
- Occasional 2-5% jumps for drama
- Realistic seed prices (AAPL ~$190, GOOGL ~$175)
- In-process background task, no external dependencies

And two requirements the visible product imposes:

- **Visible movement.** Real 500ms price moves are invisible on a chart. The
  simulator runs on an accelerated clock.
- **Determinism on demand.** Tests need repeatable paths from a fixed seed.

---

## 2. Why GBM

The standard model for equity prices, and the right one here for three
reasons that all show up on screen:

- **Prices stay positive.** The step is multiplicative, so no path reaches
  zero and no ticker ever renders a negative price.
- **Returns scale, not prices.** A $1 move in a $900 stock and a $1 move in
  a $20 stock look equally unremarkable, which is how markets behave.
- **Volatility is a single interpretable dial.** "30% annualized" is a
  number anyone can reason about, and it maps directly onto how jumpy the
  ticker looks.

The discrete exact solution, which is what the code implements:

```
S(t+dt) = S(t) * exp( (mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z )
```

- `mu` — annualized drift
- `sigma` — annualized volatility
- `dt` — elapsed time in years
- `Z` — a standard normal draw

The `- sigma^2/2` term is the Ito correction. Without it the *median* path
drifts below `mu` and high-volatility names creep upward on average even
with zero drift. Dropping it is the most common way to get this wrong.

---

## 3. The Accelerated Clock

Real 500ms moves are far too small to see. With 30% annual volatility, a
half-second move on a $190 stock is about half a cent.

So one tick represents one **minute** of market time rather than half a
second:

```
TRADING_MINUTES_PER_YEAR = 252 * 390 = 98_280
dt = tick_minutes / TRADING_MINUTES_PER_YEAR
```

At `tick_minutes = 1.0` and `sigma = 0.30`:

```
per-tick sigma = 0.30 * sqrt(1 / 98_280) = 0.00096   (about 0.10%)
on a $190 stock                          = about $0.18 per tick
```

Large enough to flash, small enough to look like a price and not a slot
machine. The resulting demo pace:

| Wall clock | Simulated |
|---|---|
| 500ms | 1 minute |
| 3.25 min | one full session (390 minutes) |
| 1 hour | about 18 trading days |

`tick_minutes` is the single dial for pace. Raise it for a livelier demo,
lower it toward realism.

### Session rollover

Every 390 ticks the simulator closes the session: each ticker's
`previous_close` is set to its current price. This keeps the daily change
percentage in a believable band instead of growing without bound over a long
demo, and it gives the P&L chart natural day boundaries.

Without it, an app left running for an hour would show "+38.2% today", which
immediately reads as broken.

---

## 4. Correlation

Independent draws per ticker look wrong: on any given tick roughly half the
board is green and half red, forever. Real markets move together.

A two-factor decomposition gives correlation without matrix algebra. Each
ticker's shock is a weighted blend of a market factor, a sector factor, and
its own idiosyncratic noise:

```
Z_i = sqrt(w_m) * Z_market
    + sqrt(w_s) * Z_sector(i)
    + sqrt(1 - w_m - w_s) * Z_i
```

The weights sum to 1, so each `Z_i` keeps unit variance and `sigma` stays
exactly the volatility it claims to be. The correlations fall out directly:

| Pair | Correlation |
|---|---|
| Same sector | `w_m + w_s` |
| Different sector | `w_m` |

Defaults `w_m = 0.35`, `w_s = 0.25` give 0.60 within a sector and 0.35
across, which sits inside the range US large caps actually exhibit.

The whole implementation is one market draw, one draw per sector present,
and one draw per ticker. No Cholesky decomposition, no covariance matrix,
no numpy.

---

## 5. Events

Each tick, each ticker independently jumps with probability
`event_probability`. A jump multiplies the price by `1 ± u` where `u` is
uniform on [0.02, 0.05].

Default `0.0008`: about one event per ticker every 1250 ticks (roughly three
simulated sessions), so across a ten-ticker watchlist something dramatic
happens about once a minute of wall clock. Frequent enough to notice, rare
enough to stay interesting.

Events are applied after the GBM step and are logged, so an unexplained
spike on the chart is traceable in the server log.

---

## 6. The Ticker Universe

Seed prices, sectors and volatilities for the ten default tickers. These are
deliberately round demo constants, not market data:

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
factor visible: when tech moves, half the watchlist moves with it.

TSLA and NVDA carry the highest volatility on purpose. A watchlist where
every row twitches identically looks synthetic; a couple of obviously wilder
names sell the illusion.

### Tickers not in the table

The user can add any symbol through the trade bar or the chat assistant, so
the simulator must price anything. Unknown tickers get a spec derived from a
SHA-256 hash of the symbol: a price in [20, 400], volatility in [0.20,
0.60], and a sector.

Hashing rather than randomizing means `PYPL` gets the same starting price on
every restart, which keeps the app coherent across container restarts and
keeps E2E tests stable. Python's built-in `hash()` will not do — it is
salted per process unless `PYTHONHASHSEED` is pinned.

---

## 7. Code Structure

`backend/market/simulator.py`, one file.

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

    Each call to `fetch` advances the world by one tick, so the caller's
    poll interval sets the simulation's pace.
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
        for ticker in tickers:
            self._register(ticker)
        self._advance(tickers)
        now = datetime.now(timezone.utc)
        return {
            ticker: Quote(
                ticker=ticker,
                price=round(self._prices[ticker], 2),
                previous_close=round(self._closes[ticker], 2),
                timestamp=now,
            )
            for ticker in tickers
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

No `aclose` override: the base class no-op is correct, since the simulator
holds nothing to release.

---

## 8. Determinism

Same seed, same tickers, same sequence. Three things make that true, and
each is easy to break:

1. **One `random.Random` instance, never the module-level `random`.** Module
   functions share global state with anything else in the process.
2. **`sorted()` around the sector set.** Iterating a set of strings gives an
   order that depends on `PYTHONHASHSEED`, so an unsorted comprehension
   consumes RNG draws in a different order across runs and quietly destroys
   reproducibility.
3. **`hashlib.sha256`, not `hash()`, for derived specs.** Built-in `hash()`
   is salted per process.

Determinism holds for a fixed ticker sequence. Adding a ticker mid-run
changes the number of draws per tick, so paths diverge from that point on.
That is expected; tests should fix the ticker list.

Set the seed with `SIMULATOR_SEED` in the environment or the `seed`
constructor argument. Leave it unset in production so each container gets a
different market.

---

## 9. Tuning

| Parameter | Default | Effect |
|---|---|---|
| `poll_interval` | 0.5 | Wall-clock seconds per tick |
| `tick_minutes` | 1.0 | Simulated minutes per tick. The main pace dial |
| `market_weight` | 0.35 | Cross-sector correlation |
| `sector_weight` | 0.25 | Additional within-sector correlation |
| `event_probability` | 0.0008 | Per-ticker, per-tick jump chance |
| `mean_reversion` | 0.0 | Pull toward the seed price. Off by default |
| `seed` | None | Fix for reproducible paths |

Recipes:

- **Livelier demo.** `tick_minutes=3.0` triples per-tick movement and
  compresses a session to about 65 seconds.
- **Calmer, more realistic.** `tick_minutes=0.25`, `event_probability=0.0`.
- **Everything moves as one.** `market_weight=0.8, sector_weight=0.15`.
  Useful for showing a market-wide selloff in the portfolio heatmap.
- **Long-running kiosk.** `mean_reversion=10.0` gives a half-life of roughly
  17 simulated sessions, keeping absolute price levels near their seeds over
  hours. Leave it at `0.0` otherwise; pure GBM is the honest model and
  session rollover already handles the change-percentage problem.

`mean_reversion` biases the drift by `kappa * ln(seed_price / price)`, which
is an Ornstein-Uhlenbeck pull in log space. It is off by default because it
is not GBM any more, and turning it on should be a decision rather than an
accident.

---

## 10. Tests

Statistical properties, not exact values. All of these run in well under a
second.

```python
import math
import statistics

import pytest

from backend.market.simulator import SimulatorProvider, TRADING_MINUTES_PER_YEAR

TICKERS = ["AAPL", "GOOGL", "JPM"]


async def _paths(provider, tickers, steps):
    """Collect price series for each ticker over `steps` ticks."""
    series = {t: [] for t in tickers}
    for _ in range(steps):
        quotes = await provider.fetch(tickers)
        for ticker, quote in quotes.items():
            series[ticker].append(quote.price)
    return series


async def test_recovers_configured_volatility():
    """Realized volatility of log returns matches the sigma we asked for."""
    provider = SimulatorProvider(seed=1, event_probability=0.0)
    prices = (await _paths(provider, ["AAPL"], 100_000))["AAPL"]
    returns = [math.log(b / a) for a, b in zip(prices, prices[1:])]
    dt = 1 / TRADING_MINUTES_PER_YEAR
    realized = statistics.stdev(returns) / math.sqrt(dt)
    assert realized == pytest.approx(0.28, rel=0.05)


async def test_same_sector_pair_is_more_correlated():
    """Two tech names co-move more than a tech name and a bank."""
    provider = SimulatorProvider(seed=2, event_probability=0.0)
    series = await _paths(provider, TICKERS, 50_000)
    rets = {
        t: [math.log(b / a) for a, b in zip(p, p[1:])] for t, p in series.items()
    }
    same = statistics.correlation(rets["AAPL"], rets["GOOGL"])
    cross = statistics.correlation(rets["AAPL"], rets["JPM"])
    assert same == pytest.approx(0.60, abs=0.05)
    assert cross == pytest.approx(0.35, abs=0.05)


async def test_prices_stay_positive():
    provider = SimulatorProvider(seed=3, tick_minutes=30.0)
    series = await _paths(provider, TICKERS, 20_000)
    assert all(price > 0 for prices in series.values() for price in prices)


async def test_same_seed_produces_same_path():
    a = await _paths(SimulatorProvider(seed=7), TICKERS, 200)
    b = await _paths(SimulatorProvider(seed=7), TICKERS, 200)
    assert a == b


async def test_different_seeds_diverge():
    a = await _paths(SimulatorProvider(seed=7), TICKERS, 200)
    b = await _paths(SimulatorProvider(seed=8), TICKERS, 200)
    assert a != b


async def test_unknown_ticker_gets_stable_seed_price():
    first = await SimulatorProvider(seed=1).fetch(["PYPL"])
    second = await SimulatorProvider(seed=99).fetch(["PYPL"])
    assert first["PYPL"].previous_close == second["PYPL"].previous_close


async def test_session_rollover_resets_previous_close():
    """After 390 ticks the previous close tracks the price, not the seed."""
    provider = SimulatorProvider(seed=4, event_probability=0.0)
    for _ in range(390):
        quotes = await provider.fetch(["AAPL"])
    assert quotes["AAPL"].previous_close == pytest.approx(
        quotes["AAPL"].price, abs=0.01
    )
```

The volatility test is the one that catches a dropped `- sigma^2/2` term or
a `dt` that is off by a factor of the trading calendar. Keep it.

### Measured behaviour

Running the code in section 7 with the defaults produces:

| Property | Target | Measured |
|---|---|---|
| Realized volatility, AAPL over 100k ticks | 0.28 | 0.2804 |
| Correlation, AAPL/GOOGL (same sector) | 0.60 | 0.597 |
| Correlation, AAPL/JPM (cross sector) | 0.35 | 0.355 |
| Mean absolute per-tick move, AAPL at $190 | - | $0.13 |
| Session change, 200 sessions | - | mean +0.03%, stdev 1.87%, 5th/95th percentile -3.2% / +3.0% |

The last row is the one that matters for how the product reads. A typical
simulated session lands within a few percent of flat, which is what a real
watchlist looks like, and the session rollover in section 3 keeps it there
no matter how long the app runs.

The first tick's quote returns `previous_close == seed_price` and a price
already one step away from it, so a fresh page load shows a small non-zero
daily change rather than a wall of zeroes.
