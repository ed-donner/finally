# Market Simulator Design

Approach and code structure for simulating realistic stock prices when no `MASSIVE_API_KEY` is configured.

This document describes the simulator **as implemented** in `backend/app/market/simulator.py` and `seed_prices.py`. It supersedes `planning/archive/MARKET_SIMULATOR.md`, which was a pre-implementation sketch; one correction is called out below where the shipped code fixes a logic bug present in the original sketch.

## Overview

The simulator uses **Geometric Brownian Motion (GBM)** to generate realistic stock price paths — the standard model underlying Black-Scholes option pricing. Prices evolve continuously with random noise, can't go negative, and exhibit the lognormal distribution seen in real markets.

Updates run at ~500ms intervals (`SimulatorDataSource`'s default `update_interval`), producing a continuous stream of price changes that feel alive.

## GBM Math

At each time step, a stock price evolves as:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

Where:
- `S(t)` = current price
- `mu` = annualized drift (expected return), e.g. 0.05 (5%)
- `sigma` = annualized volatility, e.g. 0.20 (20%)
- `dt` = time step as a fraction of a trading year
- `Z` = correlated standard normal random variable

`dt` is derived, not hardcoded, from the actual trading-calendar assumption:

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800 (252 trading days, 6.5h/day)
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8, for 500ms ticks
```

This tiny `dt` produces small, sub-cent moves per tick that accumulate naturally over time into realistic intraday ranges.

## Correlated Moves

Real stocks don't move independently — tech stocks tend to move together, etc. The simulator uses a **Cholesky decomposition** of a correlation matrix to turn independent random draws into correlated ones.

Given a correlation matrix `C`, compute `L = cholesky(C)`. Then for independent standard normals `Z_independent`:

```
Z_correlated = L @ Z_independent
```

Correlation groups, defined in `seed_prices.py`:

```python
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6      # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR = 0.3     # Between sectors / unknown tickers
TSLA_CORR = 0.3            # TSLA does its own thing
```

### Pairwise correlation lookup — and a bug the shipped code fixes

`GBMSimulator._pairwise_correlation` checks TSLA **before** sector membership:

```python
@staticmethod
def _pairwise_correlation(t1: str, t2: str) -> float:
    tech = CORRELATION_GROUPS["tech"]
    finance = CORRELATION_GROUPS["finance"]

    # TSLA is in the tech set but behaves independently — checked FIRST
    if t1 == "TSLA" or t2 == "TSLA":
        return TSLA_CORR

    if t1 in tech and t2 in tech:
        return INTRA_TECH_CORR
    if t1 in finance and t2 in finance:
        return INTRA_FINANCE_CORR

    return CROSS_GROUP_CORR
```

`TSLA` is a member of the `"tech"` set (so it still participates in the default cross-group baseline), but the design intent — "TSLA does its own thing, correlation ~0.3 with everything" — requires the TSLA check to run *before* the tech-membership check. The archived sketch (`planning/archive/MARKET_SIMULATOR.md`) checked tech/finance membership first and TSLA last, which meant the TSLA branch was unreachable dead code: for any TSLA/other-tech pair, `t1_tech and t2_tech` would already be `True` (since TSLA ∈ tech) and return `0.6` before the TSLA-specific check was ever reached. The shipped implementation orders the checks correctly.

## Random Events

Every step, each ticker has a small probability (`event_probability`, default `0.001`) of a random event — a sudden 2-5% move — for visual drama:

```python
if random.random() < self._event_prob:
    shock_magnitude = random.uniform(0.02, 0.05)
    shock_sign = random.choice([-1, 1])
    self._prices[ticker] *= 1 + shock_magnitude * shock_sign
```

At 0.1% chance per tick per ticker, with 10 tickers at 2 ticks/sec, expect an event roughly every 50 seconds — frequent enough to keep the dashboard interesting without feeling chaotic.

## Seed Prices & Per-Ticker Parameters

`backend/app/market/seed_prices.py`:

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
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}
```

Tickers added dynamically (not in `SEED_PRICES`) start at `random.uniform(50.0, 300.0)` and use `DEFAULT_PARAMS`.

## Implementation

`GBMSimulator` (pure, synchronous — no asyncio, no I/O):

```python
class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR

    def __init__(self, tickers, dt=DEFAULT_DT, event_probability=0.001):
        self._dt = dt
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict] = {}
        self._cholesky: np.ndarray | None = None
        for ticker in tickers:
            self._add_ticker_internal(ticker)   # no Cholesky rebuild per-ticker
        self._rebuild_cholesky()                # ...one rebuild after batch init

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Hot path — called every 500ms."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result = {}
        for i, ticker in enumerate(self._tickers):
            mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= (1 + shock)

            result[ticker] = round(self._prices[ticker], 2)
        return result

    def add_ticker(self, ticker: str) -> None:
        """Rebuilds the Cholesky matrix — O(n^2), but n stays small (<50 tickers)."""
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
        """Adds price/params state without rebuilding Cholesky — used for batch init."""
        if ticker in self._prices:
            return
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
                corr[i, j] = corr[j, i] = rho
        self._cholesky = np.linalg.cholesky(corr)
```

`__init__` batches ticker setup via the private `_add_ticker_internal` (no Cholesky rebuild) followed by a single `_rebuild_cholesky()` call — avoiding `O(n)` redundant `O(n^2)` rebuilds when constructing with the full default watchlist. The public `add_ticker()` rebuilds on every call since tickers are added one at a time after startup.

## `SimulatorDataSource` — the async wrapper

`GBMSimulator` itself is synchronous and has no knowledge of the `PriceCache` or asyncio. `SimulatorDataSource` (also in `simulator.py`) is the `MarketDataSource` implementation that owns the background task and writes results into the cache — see `planning/MARKET_INTERFACE.md` for its full lifecycle (including the immediate-seed-on-start/add-ticker behavior). In short:

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache, update_interval=0.5, event_probability=0.001):
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        for ticker in tickers:                       # seed cache before first tick
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def _run_loop(self) -> None:
        while True:
            try:
                for ticker, price in self._sim.step().items():
                    self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")   # loop survives a bad step
            await asyncio.sleep(self._interval)
```

## File Structure

```
backend/
  app/
    market/
      simulator.py       # GBMSimulator (pure math) + SimulatorDataSource (async wrapper)
      seed_prices.py      # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS,
                           # CORRELATION_GROUPS, INTRA_TECH_CORR, INTRA_FINANCE_CORR,
                           # CROSS_GROUP_CORR, TSLA_CORR
```

`seed_prices.py` holds only constant dictionaries/sets — no logic. `simulator.py` holds both the GBM math (`GBMSimulator`) and the `MarketDataSource` implementation that drives it on a timer (`SimulatorDataSource`); they're kept in one module because `SimulatorDataSource` is a thin wrapper with no independent reason to exist in its own file.

## Behavior Notes

- Prices never go negative — GBM is multiplicative (`exp()` is always positive)
- The tiny `dt` produces sub-cent moves per tick, which accumulate naturally over time into realistic-looking intraday ranges
- With `sigma=0.50` (TSLA), a simulated trading day produces roughly the right intraday range
- The correlation matrix must be positive semi-definite for `np.linalg.cholesky` to succeed — guaranteed here since all pairwise correlations are fixed positive constants (0.3-0.6) and the diagonal is 1.0, which keeps the matrix well within the valid PSD range for any `n`
- Random events happen ~0.1% of steps ≈ once every ~500 seconds per ticker; with 10 tickers, expect a visible event roughly every 50 seconds
- Adding/removing a ticker mid-session rebuilds the Cholesky matrix — `O(n^2)`, but `n` stays small (<50 tickers), so this is not a performance concern
- `GBMSimulator.step()` is the hot path (called every 500ms by `SimulatorDataSource._run_loop`) and is kept allocation-light: one `np.random.standard_normal(n)` call, one matrix-vector multiply, then a per-ticker loop with no further NumPy calls

## Test Coverage

`test_simulator.py` (17 tests, 98% coverage of `simulator.py`) and `test_simulator_source.py` (10 integration tests) in `backend/tests/market/` — see `planning/MARKET_DATA_SUMMARY.md` for the full test inventory.
