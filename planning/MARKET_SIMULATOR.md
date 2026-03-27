# Market Simulator Design

Approach and full code structure for the `GBMSimulator` — the built-in stock price simulator used when no `MASSIVE_API_KEY` is configured.

## Overview

The simulator uses **Geometric Brownian Motion (GBM)** to generate realistic stock price paths. GBM is the standard model underlying Black-Scholes option pricing: prices evolve multiplicatively with random noise, can never go negative, and produce the lognormal distribution observed in real equity markets.

Key properties:
- Updates at **~500ms intervals** — prices feel alive on the dashboard
- **Correlated moves** — tech stocks tend to move together (Cholesky decomposition)
- **Random events** — occasional 2–5% shocks add drama and visual interest
- **Realistic volatility** — each ticker has tuned `sigma` reflecting real-world behavior
- **Hot-swappable tickers** — add/remove tickers at runtime with correlation matrix rebuilt automatically

---

## GBM Mathematics

At each discrete time step, a stock price evolves as:

```
S(t + dt) = S(t) × exp((μ − σ²/2) × dt + σ × √dt × Z)
```

Where:
- `S(t)` = current price
- `μ` (mu) = annualized drift (expected return), e.g. `0.05` = 5%/year
- `σ` (sigma) = annualized volatility, e.g. `0.20` = 20%/year
- `dt` = time step as a fraction of a trading year
- `Z ~ N(0, 1)` = standard normal random variable

**Why `exp()`?** GBM is multiplicative, so prices can never hit zero or go negative — a fundamental requirement for realistic stock simulation.

**Calculating `dt` for 500ms updates:**

```
dt = 0.5 seconds / (252 trading days × 6.5 hours/day × 3600 seconds/hour)
   = 0.5 / 5,896,800
   ≈ 8.48e-8
```

This tiny `dt` produces realistic sub-cent moves per tick that accumulate naturally over a simulated trading session.

---

## Correlated Moves via Cholesky Decomposition

Real stocks don't move independently. Tech stocks move together; banks correlate with each other; TSLA is its own animal. We model this using a correlation matrix and Cholesky decomposition.

**Algorithm:**

1. Define an `n × n` correlation matrix `C` for the active tickers
2. Compute the lower-triangular Cholesky factor `L` such that `C = L × Lᵀ`
3. At each step, draw `n` independent standard normals: `Z_ind ~ N(0, 1)ⁿ`
4. Produce correlated normals: `Z_corr = L × Z_ind`
5. Use `Z_corr[i]` as the `Z` in the GBM formula for ticker `i`

**Default correlation groups:**

| Group | Tickers | Intra-group ρ |
|-------|---------|--------------|
| Tech | AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX | 0.60 |
| Finance | JPM, V | 0.50 |
| TSLA | TSLA | 0.30 (cross-group) |
| Cross-group | any tech ↔ finance | 0.30 |
| Unknown tickers | all others | 0.30 |

The correlation matrix must be positive semi-definite for Cholesky to work. Our symmetric, diagonally-dominant construction guarantees this.

---

## Random Events

Every 500ms step, each ticker independently has a small chance (`0.001` = 0.1%) of a sudden price shock — a rapid 2–5% move in either direction.

```python
if random.random() < event_probability:
    shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
    price *= (1 + shock)
```

With 10 tickers at 0.1% per step, expect roughly one event somewhere in the watchlist every **~50 seconds**. This is frequent enough to keep the dashboard visually interesting without being cartoonish.

---

## Seed Prices

Realistic starting prices for the default watchlist tickers (as of early 2026):

```python
SEED_PRICES: dict[str, float] = {
    "AAPL":  190.0,
    "GOOGL": 175.0,
    "MSFT":  420.0,
    "AMZN":  185.0,
    "TSLA":  250.0,
    "NVDA":  800.0,
    "META":  500.0,
    "JPM":   195.0,
    "V":     280.0,
    "NFLX":  600.0,
}

# Tickers not in SEED_PRICES start at a random price in this range
UNKNOWN_TICKER_PRICE_RANGE = (50.0, 300.0)
```

---

## Per-Ticker Parameters

Each default ticker has tuned volatility to reflect real-world behavior:

```python
TICKER_PARAMS: dict[str, dict] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High vol, lower drift — erratic
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High vol, strong upward drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Default parameters for any ticker not in the table above
DEFAULT_PARAMS: dict = {"sigma": 0.25, "mu": 0.05}
```

**Volatility intuition**: with `sigma=0.50` (TSLA) and correct `dt`, a simulated trading day produces roughly the right intraday range you'd see on a real TSLA chart.

---

## Full Implementation

### `seed_prices.py` (constants only)

```python
# backend/app/market/seed_prices.py

SEED_PRICES: dict[str, float] = {
    "AAPL":  190.0,
    "GOOGL": 175.0,
    "MSFT":  420.0,
    "AMZN":  185.0,
    "TSLA":  250.0,
    "NVDA":  800.0,
    "META":  500.0,
    "JPM":   195.0,
    "V":     280.0,
    "NFLX":  600.0,
}

TICKER_PARAMS: dict[str, dict] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},
    "NVDA":  {"sigma": 0.40, "mu": 0.08},
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},
    "V":     {"sigma": 0.17, "mu": 0.04},
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict = {"sigma": 0.25, "mu": 0.05}

UNKNOWN_TICKER_PRICE_RANGE: tuple[float, float] = (50.0, 300.0)

# Correlation groups (used by GBMSimulator._get_correlation)
TECH_TICKERS: frozenset[str] = frozenset({"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"})
FINANCE_TICKERS: frozenset[str] = frozenset({"JPM", "V"})
CORRELATIONS: dict = {
    "intra_tech":    0.60,
    "intra_finance": 0.50,
    "cross_sector":  0.30,
    "tsla":          0.30,   # TSLA with everything
    "default":       0.30,
}
```

---

### `gbm.py` (pure math, no async)

```python
# backend/app/market/gbm.py
import math
import random
import numpy as np
from .seed_prices import (
    SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS,
    UNKNOWN_TICKER_PRICE_RANGE, TECH_TICKERS, FINANCE_TICKERS, CORRELATIONS,
)

class GBMSimulator:
    """
    Generates correlated Geometric Brownian Motion price paths for multiple tickers.

    This class is pure math with no async or I/O — wrap it in SimulatorDataSource
    (see simulator.py) for use with FastAPI.
    """

    # dt = 0.5s / (252 days * 6.5 hrs * 3600 s) — one 500ms tick as a fraction of a trading year
    DEFAULT_DT: float = 0.5 / (252 * 6.5 * 3600)

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ):
        self._dt = dt
        self._event_prob = event_probability
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict] = {}
        self._tickers: list[str] = []
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self.add_ticker(ticker)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the simulation. Rebuilds the correlation matrix."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(
            ticker,
            random.uniform(*UNKNOWN_TICKER_PRICE_RANGE),
        )
        self._params[ticker] = TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the simulation. Rebuilds the correlation matrix."""
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """
        Advance one time step. Returns a dict of {ticker: new_price}.
        Modifies internal prices in place.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        # Generate correlated standard normal draws
        z_ind = np.random.standard_normal(n)
        z = self._cholesky @ z_ind if self._cholesky is not None else z_ind

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu: float = params["mu"]
            sigma: float = params["sigma"]

            # GBM formula: S(t+dt) = S(t) * exp(drift + diffusion)
            drift = (mu - 0.5 * sigma ** 2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * float(z[i])
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random event: occasional sudden 2-5% shock
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= (1.0 + shock)

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def get_price(self, ticker: str) -> float | None:
        """Get the current simulated price for a ticker."""
        return self._prices.get(ticker)

    def current_prices(self) -> dict[str, float]:
        """Return a copy of all current simulated prices."""
        return dict(self._prices)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rebuild_cholesky(self) -> None:
        """Recompute the Cholesky factor of the correlation matrix."""
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        # Build symmetric correlation matrix
        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._get_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        try:
            self._cholesky = np.linalg.cholesky(corr)
        except np.linalg.LinAlgError:
            # Fallback: identity (no correlation) if matrix is not positive definite
            self._cholesky = None

    def _get_correlation(self, t1: str, t2: str) -> float:
        """Return the pairwise correlation coefficient for two tickers."""
        # TSLA is a loner — low correlation with everything
        if t1 == "TSLA" or t2 == "TSLA":
            return CORRELATIONS["tsla"]

        t1_tech = t1 in TECH_TICKERS
        t2_tech = t2 in TECH_TICKERS
        t1_fin = t1 in FINANCE_TICKERS
        t2_fin = t2 in FINANCE_TICKERS

        if t1_tech and t2_tech:
            return CORRELATIONS["intra_tech"]
        if t1_fin and t2_fin:
            return CORRELATIONS["intra_finance"]
        if (t1_tech and t2_fin) or (t1_fin and t2_tech):
            return CORRELATIONS["cross_sector"]

        return CORRELATIONS["default"]
```

---

### `simulator.py` (async wrapper)

```python
# backend/app/market/simulator.py
import asyncio
import logging
from .interface import MarketDataSource, PriceCache
from .gbm import GBMSimulator

logger = logging.getLogger(__name__)


class SimulatorDataSource(MarketDataSource):
    """
    Async MarketDataSource that drives a GBMSimulator and writes prices to PriceCache.
    Ticks every `update_interval` seconds (default 500ms).
    """

    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5):
        self._cache = price_cache
        self._interval = update_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._sim: GBMSimulator | None = None

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(tickers)
        self._sim = GBMSimulator(tickers=self._tickers)
        # Seed the cache immediately so SSE clients have prices on first connect
        for ticker, price in self._sim.current_prices().items():
            self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop())
        logger.info("SimulatorDataSource started with %d tickers", len(self._tickers))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("SimulatorDataSource stopped")

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            if self._sim:
                self._sim.add_ticker(ticker)
                price = self._sim.get_price(ticker)
                if price is not None:
                    self._cache.update(ticker=ticker, price=price)

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _run_loop(self) -> None:
        while True:
            prices = self._sim.step()
            for ticker, price in prices.items():
                self._cache.update(ticker=ticker, price=price)
            await asyncio.sleep(self._interval)
```

---

## File Structure

```
backend/
  app/
    market/
      __init__.py           # Exports for convenient imports
      models.py             # PriceUpdate dataclass
      interface.py          # MarketDataSource ABC + PriceCache
      factory.py            # create_market_data_source()
      massive_client.py     # MassiveDataSource (REST polling)
      simulator.py          # SimulatorDataSource (async loop)
      gbm.py                # GBMSimulator (pure math, no async)
      seed_prices.py        # SEED_PRICES, TICKER_PARAMS, constants
```

---

## Behavior Notes

### Price floors and ceilings
Prices can never go negative because GBM uses `exp()`, which is always positive. There is no hard ceiling — with high enough volatility, a stock could theoretically drift to extreme values over a very long session. In practice, with `sigma ≤ 0.50` and a finite session, this is not a problem.

### New tickers
Unknown tickers (not in `SEED_PRICES`) start at a random price between $50 and $300. This is intentional: it creates interesting variety when the user adds a custom ticker.

### Cholesky rebuild cost
When a ticker is added or removed, the correlation matrix is rebuilt. This is `O(n²)` but `n` stays small (under 50 tickers in normal use), making it negligible.

### What `dt` means in practice
With `dt ≈ 8.5e-8` and `sigma=0.20` (MSFT):
- Per-tick volatility = `sigma × sqrt(dt)` = `0.20 × 0.000291` ≈ **0.0058%** per step
- After 1 simulated minute (120 steps at 0.5s): `0.0058% × sqrt(120)` ≈ **0.064%**
- This produces a calm, realistic-looking chart

With `sigma=0.50` (TSLA), ticks are ~2.5x larger, producing the erratic behavior you'd expect.

### Random events
The `event_probability=0.001` means each ticker fires an event on average once every 1,000 steps = every **500 seconds**. With 10 tickers, expect a visible price shock somewhere in the watchlist roughly every **50 seconds** — enough to keep the dashboard lively.

### Correlation validity
The hardcoded correlations (`0.60`, `0.50`, `0.30`) produce a valid positive semi-definite matrix for the default 10-ticker watchlist. For unusual combinations of many unknown tickers, the `_rebuild_cholesky` method catches `LinAlgError` and falls back to uncorrelated (identity) behavior.
