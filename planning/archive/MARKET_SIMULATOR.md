# Market Simulator

The built-in price simulator FinAlly uses when `MASSIVE_API_KEY` is not set (the
default for most users). It generates believable, dramatic price action with no
external dependencies, running as an in-process background task.

It implements the `MarketDataSource` interface from `MARKET_INTERFACE.md` — the
rest of the app cannot tell whether prices come from here or from Massive.

---

## 1. Goals

- **Realistic-looking motion.** Prices wander like real stocks, not random noise.
- **Dramatic enough for a demo.** Visible upticks/downticks every ~500ms, with
  occasional sharp moves so the terminal feels alive.
- **Correlated tickers.** Tech names tend to move together, so the watchlist
  breathes as a market rather than 10 independent random walks.
- **Deterministic when needed.** A seedable RNG so E2E tests can assert behavior.
- **Zero dependencies, in-process.** Pure Python + `random` (optionally NumPy for
  speed; not required at 10 tickers).
- **Simple.** One small engine class. No market calendar, no order book.

---

## 2. The model — Geometric Brownian Motion (GBM)

Real equity prices are modeled well by GBM, which keeps prices positive and
makes returns (not absolute prices) the random quantity. One discrete step:

```
S(t+dt) = S(t) * exp( (mu - 0.5 * sigma^2) * dt + sigma * sqrt(dt) * Z )
```

where:

- `S(t)` — current price
- `mu` — annual drift (expected return); small, per-ticker
- `sigma` — annual volatility; per-ticker (tech > financials)
- `dt` — time step as a fraction of a trading year
- `Z` — a standard normal random draw (this is where correlation enters)

### Choosing `dt`

The simulator steps every 500ms. Scaling to a trading year keeps `mu`/`sigma` in
familiar annualized units:

```
seconds_per_trading_year = 252 * 6.5 * 3600   # 252 trading days x 6.5h
dt = 0.5 / seconds_per_trading_year
```

With a realistic `sigma` (~0.3), per-step moves are small (fractions of a
percent), which looks natural. Drama comes from the event mechanism (section 5),
not from cranking volatility.

---

## 3. Seed data

Each ticker starts from a realistic price and gets its own drift/volatility. Tech
names carry higher volatility; financials lower.

```python
# backend/market/seeds.py
from dataclasses import dataclass


@dataclass(frozen=True)
class TickerSeed:
    price: float
    mu: float          # annual drift
    sigma: float       # annual volatility
    sector: str        # for correlation grouping


SEEDS: dict[str, TickerSeed] = {
    "AAPL": TickerSeed(190.0, 0.08, 0.28, "tech"),
    "GOOGL": TickerSeed(175.0, 0.10, 0.30, "tech"),
    "MSFT": TickerSeed(420.0, 0.09, 0.26, "tech"),
    "AMZN": TickerSeed(185.0, 0.11, 0.33, "tech"),
    "TSLA": TickerSeed(250.0, 0.05, 0.55, "tech"),
    "NVDA": TickerSeed(880.0, 0.15, 0.50, "tech"),
    "META": TickerSeed(500.0, 0.10, 0.35, "tech"),
    "JPM":  TickerSeed(200.0, 0.06, 0.20, "financial"),
    "V":    TickerSeed(275.0, 0.07, 0.19, "financial"),
    "NFLX": TickerSeed(630.0, 0.09, 0.40, "tech"),
}

DEFAULT_SEED = TickerSeed(100.0, 0.07, 0.30, "other")
```

Any ticker the user adds that is not in `SEEDS` is lazily created from
`DEFAULT_SEED` (with the starting price nudged by the RNG so it is not always
exactly $100). This mirrors the ten default watchlist tickers in `PLAN.md`.

---

## 4. Correlation

To make sectors move together, each step draws one **market factor** and one
**sector factor**, then blends them with a per-ticker idiosyncratic draw:

```
Z_ticker = w_m * Z_market + w_s * Z_sector + w_i * Z_idiosyncratic
```

with weights chosen so the components combine to roughly unit variance, e.g.
`w_m = 0.5`, `w_s = 0.4`, `w_i = sqrt(1 - w_m^2 - w_s^2)`. This is a lightweight
one-factor-per-sector correlation model — enough to make the watchlist visibly
correlated without a full covariance matrix.

---

## 5. Random events (drama)

On each step, with small probability (~0.5%), a ticker gets a one-off shock — a
sudden 2-5% jump up or down — layered on top of its normal GBM move. This
produces the occasional dramatic candle that makes the terminal exciting.

```python
if rng.random() < EVENT_PROBABILITY:          # ~0.005 per ticker per step
    shock = rng.uniform(0.02, 0.05)
    price *= (1 + shock) if rng.random() < 0.5 else (1 - shock)
```

Events are independent per ticker (they punch through the correlation), so a
single name can spike while the rest of its sector drifts.

---

## 6. Engine structure

The engine owns per-ticker current prices and advances them one step per
`step()` call. `SimulatedSource.get_prices` (see `MARKET_INTERFACE.md`) is a thin
wrapper over `step()`.

```python
# backend/market/gbm.py
import math
import random
from .seeds import SEEDS, DEFAULT_SEED, TickerSeed

SECONDS_PER_YEAR = 252 * 6.5 * 3600
DT = 0.5 / SECONDS_PER_YEAR
EVENT_PROBABILITY = 0.005

# correlation weights
W_MARKET, W_SECTOR = 0.5, 0.4
W_IDIO = math.sqrt(max(0.0, 1 - W_MARKET**2 - W_SECTOR**2))


class SimEngine:
    """Advances correlated GBM prices one 500ms step at a time."""

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._prices: dict[str, float] = {}
        self._seeds: dict[str, TickerSeed] = {}

    def _ensure(self, ticker: str) -> None:
        if ticker not in self._prices:
            base = SEEDS.get(ticker)
            if base is None:
                jitter = self._rng.uniform(0.5, 2.0)
                base = TickerSeed(DEFAULT_SEED.price * jitter, DEFAULT_SEED.mu,
                                  DEFAULT_SEED.sigma, DEFAULT_SEED.sector)
            self._seeds[ticker] = base
            self._prices[ticker] = base.price

    def step(self, tickers: list[str]) -> dict[str, float]:
        for t in tickers:
            self._ensure(t)

        z_market = self._rng.gauss(0, 1)
        sector_factors: dict[str, float] = {}

        out: dict[str, float] = {}
        for t in tickers:
            seed = self._seeds[t]
            if seed.sector not in sector_factors:
                sector_factors[seed.sector] = self._rng.gauss(0, 1)
            z = (W_MARKET * z_market
                 + W_SECTOR * sector_factors[seed.sector]
                 + W_IDIO * self._rng.gauss(0, 1))

            drift = (seed.mu - 0.5 * seed.sigma**2) * DT
            diffusion = seed.sigma * math.sqrt(DT) * z
            price = self._prices[t] * math.exp(drift + diffusion)

            if self._rng.random() < EVENT_PROBABILITY:
                shock = self._rng.uniform(0.02, 0.05)
                price *= (1 + shock) if self._rng.random() < 0.5 else (1 - shock)

            price = round(max(price, 0.01), 2)
            self._prices[t] = price
            out[t] = price
        return out
```

Notes:

- **State persists across calls.** The engine remembers each price, so motion is
  a continuous walk, not fresh random values each tick.
- **Lazy seeding** handles user-added tickers without special cases.
- **`round(..., 2)`** keeps prices to cents. The `max(price, 0.01)` guard keeps
  GBM positive (it mathematically cannot hit zero, but rounding could).
- **Deterministic** when constructed with a fixed `seed` — used by E2E tests.

---

## 7. Cadence and integration

- The engine advances once per feed-loop cycle; `SimulatedSource.poll_interval_
  seconds = 0.5`, giving ~500ms updates as specified in `PLAN.md`.
- The `MarketFeed` (see `MARKET_INTERFACE.md`) computes `previous_price` and
  up/down `direction` from consecutive prices and writes `PriceTick`s to the
  shared cache. The simulator itself only needs to return `{ticker: price}`.
- Because motion is continuous, the frontend sparklines and detail chart
  accumulate a smooth, realistic-looking series from the SSE stream.

---

## 8. Testing the simulator

- **Determinism:** `SimEngine(seed=42).step([...])` twice from fresh instances
  yields identical sequences.
- **Positivity:** prices never drop to or below zero across many steps.
- **Plausible magnitude:** typical per-step return magnitude is well under 1%
  (excluding events); mean step count between events matches `EVENT_PROBABILITY`.
- **Correlation:** within a step, same-sector tickers show same-sign moves more
  often than cross-sector pairs.
- **Lazy tickers:** `step(["FOO"])` on an unseeded symbol returns a positive
  price and reuses it on the next call.

These are pure-function unit tests — no network, no async, fast and reproducible,
matching the backend testing strategy in `PLAN.md`.
