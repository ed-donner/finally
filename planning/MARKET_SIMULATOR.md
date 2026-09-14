# Market Simulator Design

Approach and code structure for simulating realistic stock prices when no `MASSIVE_API_KEY` is configured. This document reflects the actual implementation in `backend/app/market/simulator.py` and `seed_prices.py`.

## Overview

The simulator uses **Geometric Brownian Motion (GBM)** — the same model underlying Black-Scholes option pricing — to generate realistic price paths: prices evolve continuously with random noise, can never go negative, and follow the lognormal distribution seen in real markets.

`SimulatorDataSource` (see `MARKET_INTERFACE.md`) drives `GBMSimulator.step()` on a ~500ms loop, producing a continuous stream of small, natural-looking price changes.

## GBM Math

At each time step, a price evolves as:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

- `S(t)` — current price
- `mu` — annualized drift (expected return), e.g. `0.05` (5%)
- `sigma` — annualized volatility, e.g. `0.20` (20%)
- `dt` — time step as a fraction of a trading year
- `Z` — a (correlated) standard normal random draw

`dt` is derived, not hand-picked, from the actual tick interval and trading-calendar assumptions:

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600   # 5,896,800 (252 trading days * 6.5h * 3600s)
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR    # ~8.48e-8, for 500ms ticks
```

This tiny `dt` produces sub-cent moves per tick, which accumulate naturally into realistic intraday ranges over time — there's no separate "smoothing" step needed.

## Correlated Moves

Real stocks don't move independently — tech names tend to move together, etc. The simulator generates correlated random draws via **Cholesky decomposition** of a sector-based correlation matrix: given correlation matrix `C`, compute `L = cholesky(C)`, then for independent standard normals `Z_independent`, `Z_correlated = L @ Z_independent`.

Correlation structure (`seed_prices.py`):

```python
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6      # tech stocks move together
INTRA_FINANCE_CORR = 0.5   # finance stocks move together
CROSS_GROUP_CORR = 0.3     # between sectors / unknown tickers
TSLA_CORR = 0.3            # TSLA is a loner — checked first, overrides tech-group membership
```

Pairwise lookup (`GBMSimulator._pairwise_correlation`, a static method):

```python
@staticmethod
def _pairwise_correlation(t1: str, t2: str) -> float:
    tech = CORRELATION_GROUPS["tech"]
    finance = CORRELATION_GROUPS["finance"]

    if t1 == "TSLA" or t2 == "TSLA":     # checked before sector membership
        return TSLA_CORR
    if t1 in tech and t2 in tech:
        return INTRA_TECH_CORR
    if t1 in finance and t2 in finance:
        return INTRA_FINANCE_CORR
    return CROSS_GROUP_CORR
```

Note that TSLA is technically listed in the `"tech"` group set for bookkeeping purposes, but the correlation check for it comes first, so it always gets `TSLA_CORR` (0.3) rather than `INTRA_TECH_CORR` (0.6) — it moves with a weaker pull toward the rest of tech than AAPL/GOOGL/MSFT do to each other.

## Random Events

Every step, each ticker independently has a small probability of a sudden 2–5% move, for visual drama:

```python
event_probability = 0.001   # ~0.1% chance per tick per ticker

if random.random() < event_probability:
    shock_magnitude = random.uniform(0.02, 0.05)
    shock_sign = random.choice([-1, 1])
    price *= 1 + shock_magnitude * shock_sign
```

At 2 ticks/second, ~0.1% per tick per ticker works out to roughly one event every ~500 seconds *per ticker*; with the 10-ticker default watchlist, expect a visible shock somewhere roughly every ~50 seconds — often enough to keep a live dashboard interesting without making prices look erratic.

## Seed Prices

Realistic starting prices for the default watchlist, in `seed_prices.py`:

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00,
    "TSLA": 250.00, "NVDA": 800.00, "META": 500.00, "JPM": 195.00,
    "V": 280.00, "NFLX": 600.00,
}
```

A ticker added dynamically that isn't in this table starts at a random price drawn uniformly from `$50`–`$300` (`GBMSimulator._add_ticker_internal`).

## Per-Ticker Parameters

Each ticker gets its own `(sigma, mu)` to reflect real-world volatility differences:

```python
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # high vol
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # high vol, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # low vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}  # for dynamically added, unknown tickers
```

## Implementation

`GBMSimulator` (`simulator.py`) — the pure simulation engine, with no async/IO concerns of its own:

```python
class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices."""

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
            self._add_ticker_internal(ticker)   # batch init, no Cholesky rebuild per ticker
        self._rebuild_cholesky()                 # ... built once at the end

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Hot path — called every 500ms."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z_correlated = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu, sigma = params["mu"], params["sigma"]

            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock

            result[ticker] = round(self._prices[ticker], 2)
        return result

    def add_ticker(self, ticker: str) -> None:     # public: rebuilds Cholesky (single-add path)
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:  # rebuilds Cholesky
        ...

    def get_price(self, ticker: str) -> float | None: ...
    def get_tickers(self) -> list[str]: ...          # public accessor — no reaching into private state

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add without rebuilding Cholesky — used for batch init in __init__."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """O(n^2) but n stays small (well under 50 tickers in practice)."""
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

## File Structure (as built)

```
backend/
  app/
    market/
      simulator.py       # GBMSimulator + SimulatorDataSource (the MarketDataSource wrapper)
      seed_prices.py      # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, CORRELATION_GROUPS, correlation constants
```

`seed_prices.py` is pure data (constant dicts/sets, no logic). `simulator.py` holds both `GBMSimulator` (the math engine) and `SimulatorDataSource` (the async wrapper that calls `step()` on a timer and writes results into the shared `PriceCache` — see `MARKET_INTERFACE.md` for that half).

## Behavior Notes

- Prices never go negative — GBM is multiplicative (`exp()` is always positive), so there's no clamping logic needed anywhere.
- The tiny `dt` produces sub-cent moves per tick that compound naturally over many ticks rather than needing an artificial smoothing pass.
- At `sigma=0.50` (TSLA), a full simulated trading day produces roughly the intraday range you'd expect from a genuinely volatile stock; at `sigma=0.17` (V), moves are correspondingly muted.
- The correlation matrix must be positive semi-definite for `np.linalg.cholesky` to succeed — guaranteed here because it's built from a fixed, valid set of pairwise correlations (this is why an ad hoc/user-editable correlation matrix would need validation that the current fixed table doesn't).
- Adding/removing a ticker mid-session rebuilds the Cholesky decomposition. This is O(n²) but n is small, so it's cheap even on every watchlist edit.
- `GBMSimulator` itself has no `asyncio` or cache dependency — it's a plain, synchronous, easily-unit-testable class. All the async/IO/cache-writing behavior lives in `SimulatorDataSource`, which is the piece that actually implements `MarketDataSource`.

## Test Coverage

Per `planning/MARKET_DATA_SUMMARY.md`, `simulator.py` has 98% test coverage (17 tests in `test_simulator.py` + 10 integration tests in `test_simulator_source.py`). The two known uncovered lines are the `_add_ticker_internal` duplicate-ticker guard and the exception-log branch in `SimulatorDataSource._run_loop` — both defensive paths that are hard to hit deliberately in a unit test.
