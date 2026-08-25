# Market Simulator Design

Approach and code structure for simulating realistic stock prices when `MASSIVE_API_KEY` is unset — FinAlly's default mode. Implemented in `backend/app/market/simulator.py` (the `GBMSimulator` engine and the `SimulatorDataSource` adapter) and `backend/app/market/seed_prices.py` (constants).

## Overview

The simulator uses **Geometric Brownian Motion (GBM)** — the standard model underlying Black-Scholes option pricing. Prices evolve continuously with random noise, can never go negative (the update is multiplicative via `exp()`), and produce the lognormal return distribution seen in real markets, rather than a plain random walk that could drift to zero or negative.

`SimulatorDataSource` steps the simulation every `update_interval` seconds (default **0.5s**, matching the SSE cadence in `MARKET_INTERFACE.md`), producing a continuous, correlated stream of price changes.

## GBM Math

At each time step, a price evolves as:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

- `S(t)` — current price
- `mu` — annualized drift (expected return), e.g. `0.05` for 5%/year
- `sigma` — annualized volatility, e.g. `0.20` for 20%/year
- `dt` — this time step as a fraction of a trading year
- `Z` — a standard normal random draw (correlated across tickers — see below)

For 500ms ticks over a ~252-trading-day, 6.5-hour-session year:

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ≈ 8.48e-8
```

This tiny `dt` produces sub-cent moves per tick that accumulate into realistic-looking intraday ranges over minutes of wall-clock time, rather than jumping wildly on every 500ms update.

## Correlated Moves

Real stocks don't move independently — tech names tend to move together, etc. The simulator builds a **Cholesky decomposition** of a correlation matrix and uses it to turn independent normal draws into correlated ones: given correlation matrix `C`, `L = cholesky(C)`, then `Z_correlated = L @ Z_independent`. This is standard for simulating correlated GBM paths and is exact as long as `C` is a valid (positive semi-definite) correlation matrix — which a matrix built from pairwise correlations in `[0, 1)` with a unit diagonal always is.

Correlation structure, from `seed_prices.py`:

```python
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6      # tech stocks move together
INTRA_FINANCE_CORR = 0.5   # finance stocks move together
CROSS_GROUP_CORR = 0.3     # between sectors, or either ticker unknown
TSLA_CORR = 0.3            # TSLA is in the tech set but is deliberately excluded from that 0.6 — it does its own thing
```

Pairwise lookup (`GBMSimulator._pairwise_correlation`) checks TSLA first — even though it's a member of the `tech` group set, it's special-cased to `0.3` with everything, never the `0.6` intra-tech rate. Unknown tickers (dynamically added, not in either group) fall through to `CROSS_GROUP_CORR = 0.3` with everyone.

## Random Events

Every step, each ticker independently has a small probability of a sudden shock — a 2-5% jump, up or down — layered on top of the normal GBM step. This adds drama and keeps a long-running dashboard visually interesting instead of drifting flat.

```python
event_probability = 0.001  # ~0.1% chance per ticker per tick

if random.random() < event_probability:
    shock_magnitude = random.uniform(0.02, 0.05)
    shock_sign = random.choice([-1, 1])
    price *= 1 + shock_magnitude * shock_sign
```

At 2 ticks/sec, `0.001` per ticker per tick means an event on a *given* ticker roughly every 500 seconds (~8 minutes); with the 10-ticker default watchlist, expect *some* ticker to have an event roughly every 50 seconds.

## Seed Prices & Per-Ticker Parameters (`seed_prices.py`)

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00, "TSLA": 250.00,
    "NVDA": 800.00, "META": 500.00, "JPM": 195.00, "V": 280.00, "NFLX": 600.00,
}

TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # high volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # high volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}
```

Tickers added dynamically that aren't in `SEED_PRICES` start at a random price uniform in `[$50, $300]`, and use `DEFAULT_PARAMS` (`sigma=0.25`, `mu=0.05`) since there's no real-world volatility to look up for an arbitrary user-entered symbol.

## Implementation (`GBMSimulator`)

```python
import math
import random
import numpy as np


class GBMSimulator:
    """GBM simulator for correlated stock prices."""

    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR

    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT, event_probability: float = 0.001) -> None:
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
        """Advance every tracked ticker by one time step. Hot path — called every 500ms."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]

            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock

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
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """O(n^2) rebuild whenever tickers are added/removed. n stays small (<50)."""
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

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        tech, finance = CORRELATION_GROUPS["tech"], CORRELATION_GROUPS["finance"]
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

`step()` is the hot path — called on every simulator tick for every tracked ticker — so it stays allocation-light: one vectorized `numpy` draw per step for all tickers at once, rather than per-ticker `random.gauss()` calls, and the Cholesky matrix is cached and only rebuilt on `add_ticker`/`remove_ticker`, not on every `step()`.

## `SimulatorDataSource` — Wiring into the Async Loop

`GBMSimulator` itself is synchronous and has no knowledge of asyncio, the cache, or timing — `SimulatorDataSource` (in the same `simulator.py` module) is the thin `MarketDataSource` adapter that owns the background task and writes results into the shared `PriceCache` (full contract in `MARKET_INTERFACE.md`):

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5, event_probability: float = 0.001) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

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

The `try/except Exception` around the step (rather than letting an exception propagate and kill the task) matters: a single bad draw shouldn't silently stop all price updates for the rest of the process — the loop logs and keeps going on the next tick.

## File Structure

```
backend/
  app/
    market/
      simulator.py       # GBMSimulator (pure math engine) + SimulatorDataSource (async adapter)
      seed_prices.py       # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, correlation constants
```

`seed_prices.py` holds only constant dictionaries — no logic — so tuning starting prices, per-ticker volatility, or sector correlations never requires touching `simulator.py`.

## Behavior Notes

- Prices never go negative — GBM's `exp()` update is always positive, unlike an additive random walk
- The tiny `dt` produces sub-cent moves per tick that accumulate naturally into realistic multi-minute price action
- With `sigma=0.50` (TSLA) and 2 ticks/sec, a simulated trading session produces roughly the right intraday range for a genuinely volatile stock
- The correlation matrix is guaranteed positive semi-definite by construction (pairwise correlations in `[0.3, 0.6]`, unit diagonal), so `np.linalg.cholesky` never raises — no need to defensively catch a non-PSD failure
- Cholesky rebuild is `O(n^2)` but `n` stays under ~50 (the watchlist cap of 20 plus any held positions), so it's cheap even on every watchlist edit
- `round(self._prices[ticker], 2)` in `step()`'s return value only rounds what's *emitted* to the cache each tick — the simulator's internal `self._prices[ticker]` stays unrounded so tiny per-tick drifts aren't lost to repeated rounding error over a long-running session
