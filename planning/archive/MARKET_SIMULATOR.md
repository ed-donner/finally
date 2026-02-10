# Market Simulator Design

Approach and code structure for simulating realistic stock prices when no Massive API key is configured.

## Overview

The simulator uses **Geometric Brownian Motion (GBM)** to generate realistic stock price paths. GBM is the standard model underlying Black-Scholes option pricing — prices evolve continuously with random noise, can't go negative, and exhibit the lognormal distribution seen in real markets.

Updates run at ~500ms intervals, producing a continuous stream of price changes that feel alive.

## GBM Math

At each time step, a stock price evolves as:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

Where:
- `S(t)` = current price
- `mu` = annualized drift (expected return), e.g. 0.05 (5%)
- `sigma` = annualized volatility, e.g. 0.20 (20%)
- `dt` = time step as fraction of a trading year
- `Z` = standard normal random variable (drawn from N(0,1))

For our 500ms updates with ~252 trading days and ~6.5 hours per day:
```
dt = 0.5 / (252 * 6.5 * 3600) = ~8.5e-8
```

This tiny `dt` produces small, realistic per-tick moves.

## Correlated Moves

Real stocks don't move independently — tech stocks tend to move together, etc. We use a **Cholesky decomposition** of a correlation matrix to generate correlated random draws.

Given a correlation matrix `C`, compute `L = cholesky(C)`. Then for independent standard normals `Z_independent`:
```
Z_correlated = L @ Z_independent
```

Default correlation groups:
- **Tech**: AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX — corr ~0.6 within group
- **Finance**: JPM, V — corr ~0.5 within group
- **Cross-group**: ~0.3 baseline correlation
- **TSLA**: lower correlation with everything (~0.3) — it does its own thing

## Random Events

Every step, each ticker has a small probability (~0.001) of a random event — a sudden 2-5% move. This adds drama and makes the dashboard visually interesting.

```python
if random.random() < event_probability:
    shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
    price *= (1 + shock)
```

## Seed Prices

Realistic starting prices for the default watchlist:

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.0,
    "GOOGL": 175.0,
    "MSFT": 420.0,
    "AMZN": 185.0,
    "TSLA": 250.0,
    "NVDA": 800.0,
    "META": 500.0,
    "JPM": 195.0,
    "V": 280.0,
    "NFLX": 600.0,
}
```

Tickers added dynamically (not in the seed list) start at a random price between $50-$300.

## Per-Ticker Parameters

Each ticker has its own volatility to reflect real-world behavior:

```python
TICKER_PARAMS: dict[str, dict] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High vol
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High vol, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Default for unknown tickers
DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}
```

## Implementation

```python
import math
import random
import time
import numpy as np

class GBMSimulator:
    """Generates correlated GBM price paths for multiple tickers."""

    def __init__(
        self,
        tickers: list[str],
        dt: float = 8.5e-8,
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

    def add_ticker(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50, 300))
        self._params[ticker] = TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance one time step. Returns {ticker: new_price}."""
        n = len(self._tickers)
        if n == 0:
            return {}

        # Generate correlated random normals
        z_independent = np.random.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z_independent
        else:
            z = z_independent

        result = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            # GBM step
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random event
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= (1 + shock)

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def _rebuild_cholesky(self) -> None:
        """Rebuild the Cholesky decomposition of the correlation matrix."""
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._get_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    def _get_correlation(self, t1: str, t2: str) -> float:
        """Return pairwise correlation between two tickers."""
        tech = {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"}
        finance = {"JPM", "V"}

        t1_tech = t1 in tech
        t2_tech = t2 in tech
        t1_fin = t1 in finance
        t2_fin = t2 in finance

        # Same sector: higher correlation
        if t1_tech and t2_tech:
            return 0.6
        if t1_fin and t2_fin:
            return 0.5

        # TSLA is a loner
        if t1 == "TSLA" or t2 == "TSLA":
            return 0.3

        # Cross-sector or unknown
        if (t1_tech and t2_fin) or (t1_fin and t2_tech):
            return 0.3

        # Default
        return 0.3
```

## File Structure

All simulator code lives in a single module:

```
backend/
  app/
    market/
      simulator.py       # GBMSimulator class + seed data + SimulatorDataSource
      seed_prices.py      # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS (constants)
```

`seed_prices.py` contains just the constant dictionaries. `simulator.py` contains the `GBMSimulator` class and the `SimulatorDataSource` (the `MarketDataSource` implementation that wraps `GBMSimulator` in an async loop).

## Behavior Notes

- Prices never go negative (GBM is multiplicative — `exp()` is always positive)
- The tiny `dt` produces sub-cent moves per tick, which accumulate naturally over time
- With `sigma=0.50` (TSLA), a day of simulated trading produces roughly the right intraday range
- The correlation matrix must be positive semi-definite — Cholesky decomposition guarantees this for valid correlation matrices
- Random events happen ~0.1% of steps = roughly once every 500 seconds per ticker. With 10 tickers, expect an event somewhere roughly every 50 seconds — enough to keep it interesting
- When a new ticker is added mid-session, the Cholesky matrix is rebuilt. This is O(n^2) but n is small (<50 tickers)
