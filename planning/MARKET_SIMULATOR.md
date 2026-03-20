# Market Simulator Design

Approach and code structure for simulating realistic stock prices when no Massive API key is configured.

---

## Overview

The simulator uses **Geometric Brownian Motion (GBM)** to generate realistic stock price paths. GBM is the standard continuous-time model underlying Black-Scholes option pricing:

- Prices evolve multiplicatively — they can never go negative
- Returns follow a lognormal distribution, matching empirical market data
- Each tick produces a small, realistic price change that accumulates naturally over time
- Correlated moves across tickers reflect real market sector dynamics

Updates run at 500ms intervals via an asyncio background task (`SimulatorDataSource`). The `GBMSimulator` class does the math; `SimulatorDataSource` wraps it in the `MarketDataSource` interface (see `MARKET_INTERFACE.md`).

---

## GBM Mathematics

At each time step, a stock price evolves as:

```
S(t+dt) = S(t) × exp((μ - σ²/2) × dt + σ × √dt × Z)
```

Where:
- `S(t)` — current price
- `μ` (mu) — annualized drift (expected return), e.g. 0.05 = 5%/year
- `σ` (sigma) — annualized volatility, e.g. 0.20 = 20%/year
- `dt` — time step as a fraction of a trading year
- `Z` — standard normal random variable N(0,1), correlated across tickers

**Time step calculation** for 500ms ticks:
```
Trading seconds per year = 252 days × 6.5 hours × 3600 s/h = 5,896,800 s
dt = 0.5 / 5,896,800 ≈ 8.48 × 10⁻⁸
```

This tiny `dt` produces sub-cent moves per tick that accumulate over time into realistic intraday ranges. A ticker with `sigma=0.50` (TSLA-level volatility) produces a ~1% intraday range after simulating one trading day.

---

## Correlated Moves

Real stocks don't move independently — tech stocks tend to move together, financials move together, etc. The simulator captures this with **Cholesky decomposition** of a correlation matrix.

**Algorithm:**
1. Build an n×n correlation matrix `C` based on sector groupings
2. Compute lower-triangular Cholesky factor `L = cholesky(C)`
3. At each step: generate n independent standard normals `Z_independent`
4. Produce correlated draws: `Z_correlated = L @ Z_independent`
5. Use `Z_correlated[i]` as the `Z` term in the GBM formula for ticker `i`

**Correlation structure:**

| Pair | Correlation |
|------|-------------|
| Tech stocks (AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX) with each other | 0.6 |
| Finance stocks (JPM, V) with each other | 0.5 |
| TSLA with anything | 0.3 |
| Tech ↔ Finance | 0.3 |
| Unknown ticker with anything | 0.3 |

TSLA is classified as tech in the `CORRELATION_GROUPS` dict but is special-cased in `_pairwise_correlation()` — it always uses 0.3, reflecting its historically low correlation with the broader market.

The Cholesky matrix is rebuilt whenever tickers are added or removed. This is O(n²) but n stays small (< 50 tickers in practice).

---

## Random Events

Every tick, each ticker has a small probability of a sudden 2–5% move — up or down — to add drama and produce the kind of sharp moves that make a trading terminal look alive.

```python
EVENT_PROBABILITY = 0.001   # 0.1% chance per tick per ticker

if random.random() < event_probability:
    shock_magnitude = random.uniform(0.02, 0.05)   # 2–5%
    shock_sign = random.choice([-1, 1])
    price *= 1 + shock_magnitude * shock_sign
```

**Expected frequency:** With 10 tickers updating at 2 ticks/second, expect roughly one event somewhere in the watchlist every 50 seconds — frequent enough to be interesting without feeling chaotic.

---

## Seed Prices and Per-Ticker Parameters

```python
# seed_prices.py

SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 800.00,
    "META": 500.00,
    "JPM":  195.00,
    "V":    280.00,
    "NFLX": 600.00,
}

TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},   # Moderate volatility
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},   # Lowest volatility in tech
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High volatility, lower drift
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low volatility (bank stock)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Sector groups for correlation
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR    = 0.6
INTRA_FINANCE_CORR = 0.5
CROSS_GROUP_CORR   = 0.3
TSLA_CORR          = 0.3
```

Tickers added dynamically (not in `SEED_PRICES`) start at a random price between $50 and $300, and use `DEFAULT_PARAMS`.

---

## Implementation

### GBMSimulator

```python
# simulator.py
import math
import random
import numpy as np

class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices."""

    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8

    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT,
                 event_probability: float = 0.001) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()  # One build for all initial tickers

    def step(self) -> dict[str, float]:
        """Advance all tickers by one dt. Returns {ticker: new_price}. Hot path."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z_correlated = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu = self._params[ticker]["mu"]
            sigma = self._params[ticker]["sigma"]

            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker and rebuild the Cholesky matrix."""
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker and rebuild the Cholesky matrix."""
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
        """Add without rebuilding Cholesky (for batch initialization)."""
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
```

### SimulatorDataSource

Wraps `GBMSimulator` in the `MarketDataSource` interface:

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
        # Seed cache immediately so SSE has prices before the first step fires
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def _run_loop(self) -> None:
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

---

## Behavioral Properties

**Prices never go negative** — GBM is multiplicative: `price × exp(...)` is always positive for any finite `drift + diffusion`.

**Sub-cent moves per tick** — With `dt ≈ 8.5e-8` and `sigma=0.25`, the per-tick standard deviation is:
```
sigma × sqrt(dt) = 0.25 × sqrt(8.5e-8) ≈ 0.000073
```
A $190 stock moves ±$0.014 per tick at 1σ. Realistic and accumulative.

**Correlation matrix must be positive semi-definite** — Cholesky decomposition requires this. For the correlation values used (0.3–0.6), the matrix is always valid. Adding arbitrary user tickers uses 0.3 cross-correlation, which is always PSD-safe.

**Cholesky rebuild cost** — O(n²) per rebuild. With n < 50 tickers, this is microseconds. Rebuilds happen on `add_ticker()` / `remove_ticker()` calls only.

**Batch initialization optimization** — `__init__` calls `_add_ticker_internal()` for all initial tickers before calling `_rebuild_cholesky()` once. Single-ticker additions call `_rebuild_cholesky()` immediately.

---

## File Structure

```
backend/app/market/
├── simulator.py       # GBMSimulator + SimulatorDataSource
└── seed_prices.py     # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, correlation constants
```

All GBM math lives in `simulator.py`. Constants live in `seed_prices.py` so they can be imported by tests and other modules without importing the GBM logic.
