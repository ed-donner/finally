# Market Simulator Design

Approach and code structure for simulating realistic stock prices when no `MASSIVE_API_KEY` is configured.

---

## Overview

The simulator uses **Geometric Brownian Motion (GBM)** — the standard model underlying Black-Scholes option pricing. Prices evolve multiplicatively with random noise, can never go negative, and produce the lognormal distribution observed in real markets.

Updates run at **500ms intervals**, producing a continuous stream of price changes that feel alive. Sector-based correlations make the watchlist behave like a real portfolio — tech stocks move together during "market events".

---

## GBM Math

At each time step, a stock price evolves as:

```
S(t+dt) = S(t) × exp((μ - σ²/2) × dt + σ × √dt × Z)
```

| Symbol | Meaning | Typical value |
|--------|---------|---------------|
| `S(t)` | Current price | — |
| `μ` (mu) | Annualized drift (expected return) | 0.04–0.08 |
| `σ` (sigma) | Annualized volatility | 0.17–0.50 |
| `dt` | Time step as fraction of a trading year | ~8.48e-8 for 500ms |
| `Z` | Standard normal random variable N(0,1) | drawn per tick |

**Computing `dt` for 500ms ticks:**
```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800 seconds
dt = 0.5 / TRADING_SECONDS_PER_YEAR           # ≈ 8.48e-8
```

This tiny `dt` produces sub-cent moves per tick that accumulate naturally into realistic daily ranges.

**Why GBM?**
- `exp()` is always positive → prices never go negative
- Multiplicative structure → percentage moves are consistent regardless of price level
- Lognormal distribution matches empirical stock returns

---

## Correlated Moves

Real stocks don't move independently — tech stocks tend to rise and fall together. We use a **Cholesky decomposition** of a sector-based correlation matrix to generate correlated random draws.

**Procedure:**
1. Build correlation matrix `C` (n × n) from sector group rules
2. Compute lower-triangular Cholesky factor: `L = cholesky(C)`, so `L @ L.T = C`
3. Each tick: draw `n` independent normals `Z_ind ~ N(0,1)`
4. Apply: `Z_corr = L @ Z_ind` — now `Z_corr` has the desired correlations
5. Use `Z_corr[i]` as the random shock for ticker `i`

**Correlation coefficients:**

| Pair | Correlation | Rationale |
|------|-------------|-----------|
| Tech ↔ Tech | 0.6 | AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX |
| Finance ↔ Finance | 0.5 | JPM, V |
| TSLA ↔ anything | 0.3 | Does its own thing |
| Cross-sector | 0.3 | Default for unknown tickers |

The correlation matrix must be positive semi-definite for Cholesky to succeed — it is, by construction (all values are valid correlations, diagonal is 1).

---

## Random Shock Events

Every tick, each ticker has a 0.1% chance (`event_probability=0.001`) of a sudden 2–5% move:

```python
if random.random() < event_probability:
    shock_magnitude = random.uniform(0.02, 0.05)
    shock_sign = random.choice([-1, 1])
    price *= (1 + shock_magnitude * shock_sign)
```

With 10 tickers at 2 ticks/second, expect a shock event somewhere **roughly every 50 seconds**. This keeps the dashboard visually engaging and gives the AI assistant something interesting to comment on.

---

## Seed Prices and Per-Ticker Parameters

```python
# seed_prices.py

SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT":  420.00,
    "AMZN":  185.00,
    "TSLA":  250.00,
    "NVDA":  800.00,
    "META":  500.00,
    "JPM":   195.00,
    "V":     280.00,
    "NFLX":  600.00,
}

TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},  # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},  # High vol, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},  # Low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},  # Low vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR    = 0.6
INTRA_FINANCE_CORR = 0.5
CROSS_GROUP_CORR   = 0.3
TSLA_CORR          = 0.3
```

Tickers not in `SEED_PRICES` (dynamically added by the user) start at a random price between $50–$300 with `DEFAULT_PARAMS`.

---

## GBMSimulator Class

```python
# simulator.py
import math, random
import numpy as np
from .seed_prices import SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, ...

class GBMSimulator:
    """Generates correlated GBM price paths for multiple tickers."""

    DEFAULT_DT = 0.5 / (252 * 6.5 * 3600)  # ~8.48e-8

    def __init__(self, tickers: list[str], dt=DEFAULT_DT, event_probability=0.001):
        self._dt = dt
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)    # Batch add without rebuilding
        self._rebuild_cholesky()                  # One rebuild at the end

    def step(self) -> dict[str, float]:
        """Advance one tick. Returns {ticker: new_price}. Called every 500ms."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_ind = np.random.standard_normal(n)
        z = self._cholesky @ z_ind if self._cholesky is not None else z_ind

        result = {}
        for i, ticker in enumerate(self._tickers):
            mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]
            drift     = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if random.random() < self._event_prob:
                self._prices[ticker] *= 1 + random.uniform(0.02, 0.05) * random.choice([-1, 1])

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

    def _rebuild_cholesky(self) -> None:
        """Rebuild Cholesky decomposition. O(n²), n < 50 in practice."""
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
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

---

## SimulatorDataSource Class

`SimulatorDataSource` implements `MarketDataSource` by wrapping `GBMSimulator` in an asyncio loop:

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5):
        self._cache = price_cache
        self._interval = update_interval
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers)
        # Seed the cache so SSE has data immediately on first connect
        for ticker in tickers:
            if (price := self._sim.get_price(ticker)) is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def _run_loop(self) -> None:
        while True:
            try:
                prices = self._sim.step()
                for ticker, price in prices.items():
                    self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            if (price := self._sim.get_price(ticker)) is not None:
                self._cache.update(ticker=ticker, price=price)  # Immediate seed

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
```

---

## Behavior Notes

- **Prices never go negative** — GBM uses `exp()`, which is always positive
- **Sub-cent per tick** — the tiny `dt` (~8.5e-8) produces moves of ~0.001% per tick; these accumulate naturally into realistic daily ranges
- **TSLA at σ=0.50** produces roughly ±3–5% intraday variation on a typical simulated trading day — realistic for a high-vol stock
- **JPM/V at σ=0.17–0.18** produce slow, steady moves — realistic for financials
- **Cholesky rebuild** on `add_ticker`/`remove_ticker` is O(n²) but n < 50 in any realistic watchlist — imperceptible latency
- **Random events at 0.1%** per tick per ticker: with 10 tickers at 2 ticks/sec, expect ~1 event every 50 seconds across the whole watchlist
- **New tickers** (not in `SEED_PRICES`) start at `random.uniform(50, 300)` with `DEFAULT_PARAMS` (σ=0.25, μ=0.05)
- **`step()` is synchronous** — pure NumPy, no I/O. Runs directly in the asyncio event loop without `to_thread()`. At ~8.5e-8 `dt` with 10 tickers, one step takes < 0.1ms
