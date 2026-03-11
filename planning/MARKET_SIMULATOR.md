# Market Simulator Design

The simulator generates realistic stock price movements using Geometric Brownian Motion (GBM) with correlated moves, occasional events, and configurable parameters. It runs as an in-process background task with no external dependencies.

## Overview

The simulator produces price updates at ~500ms intervals for all registered tickers. It implements the `MarketDataSource` interface defined in [MARKET_INTERFACE.md](./MARKET_INTERFACE.md) so the rest of the backend is agnostic to the data source.

## Seed Prices

Realistic starting prices for the default watchlist tickers. Stored as a simple dict lookup.

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 130.00,
    "META": 500.00,
    "JPM": 195.00,
    "V": 280.00,
    "NFLX": 650.00,
}

DEFAULT_SEED_RANGE = (50.0, 300.0)  # For unknown tickers
```

When a ticker is registered that isn't in the lookup table, assign a random price uniformly sampled from `DEFAULT_SEED_RANGE`.

## Price Generation: Geometric Brownian Motion

GBM models stock prices as a stochastic process where log-returns are normally distributed. The discrete-time formula for one tick:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

Where:
- `S(t)` = current price
- `mu` = annualized drift (expected return)
- `sigma` = annualized volatility
- `dt` = time step as fraction of a year
- `Z` = standard normal random variable

### Parameters Per Ticker

```python
@dataclass
class TickerParams:
    price: float          # Current price
    mu: float             # Annualized drift (e.g., 0.08 = 8% expected annual return)
    sigma: float          # Annualized volatility (e.g., 0.25 = 25%)
    sector: str           # For correlation grouping
```

### Default Parameter Assignment

```python
TICKER_PROFILES: dict[str, dict] = {
    "AAPL":  {"mu": 0.10, "sigma": 0.22, "sector": "tech"},
    "GOOGL": {"mu": 0.08, "sigma": 0.25, "sector": "tech"},
    "MSFT":  {"mu": 0.10, "sigma": 0.20, "sector": "tech"},
    "AMZN":  {"mu": 0.12, "sigma": 0.28, "sector": "tech"},
    "TSLA":  {"mu": 0.15, "sigma": 0.50, "sector": "auto"},
    "NVDA":  {"mu": 0.15, "sigma": 0.40, "sector": "tech"},
    "META":  {"mu": 0.10, "sigma": 0.30, "sector": "tech"},
    "JPM":   {"mu": 0.06, "sigma": 0.18, "sector": "finance"},
    "V":     {"mu": 0.08, "sigma": 0.16, "sector": "finance"},
    "NFLX":  {"mu": 0.12, "sigma": 0.35, "sector": "media"},
}
```

Unknown tickers get default params: `mu=0.08`, `sigma=0.25`, `sector="other"`.

## Correlated Moves

Tickers in the same sector have correlated price movements. This makes the simulation feel realistic — tech stocks move together, financials move together.

### Implementation

Each tick:

1. Generate one shared random factor per sector: `Z_sector ~ N(0, 1)`
2. Generate one independent factor per ticker: `Z_indiv ~ N(0, 1)`
3. Combine with correlation weight `rho` (e.g., 0.6 for same-sector):
   ```
   Z_ticker = rho * Z_sector + sqrt(1 - rho^2) * Z_indiv
   ```
4. Apply GBM formula using `Z_ticker`

```python
import random
import math
from collections import defaultdict

INTRA_SECTOR_CORRELATION = 0.6


class SimulationEngine:
    """Generates correlated GBM price updates for registered tickers."""

    def __init__(self, tick_interval: float = 0.5) -> None:
        self._tickers: dict[str, TickerParams] = {}
        self._dt = tick_interval / (252 * 6.5 * 3600)  # Fraction of trading year
        self._rng = random.Random()

    def add_ticker(self, ticker: str, seed_price: float | None = None) -> None:
        ticker = ticker.upper()
        if ticker in self._tickers:
            return

        profile = TICKER_PROFILES.get(ticker, {"mu": 0.08, "sigma": 0.25, "sector": "other"})
        price = seed_price or SEED_PRICES.get(ticker)
        if price is None:
            price = self._rng.uniform(*DEFAULT_SEED_RANGE)

        self._tickers[ticker] = TickerParams(
            price=price,
            mu=profile["mu"],
            sigma=profile["sigma"],
            sector=profile["sector"],
        )

    def remove_ticker(self, ticker: str) -> None:
        self._tickers.pop(ticker.upper(), None)

    def tick(self) -> dict[str, float]:
        """Advance all tickers by one time step. Returns {ticker: new_price}."""
        if not self._tickers:
            return {}

        # Group tickers by sector
        sectors: dict[str, list[str]] = defaultdict(list)
        for ticker, params in self._tickers.items():
            sectors[params.sector].append(ticker)

        # Generate sector-level shocks
        sector_shocks = {s: self._rng.gauss(0, 1) for s in sectors}

        # Check for random event (2% chance per tick, affects one random ticker)
        event_ticker = None
        event_shock = 0.0
        if self._rng.random() < 0.02 and self._tickers:
            event_ticker = self._rng.choice(list(self._tickers.keys()))
            # Sudden 2-5% move, direction random
            magnitude = self._rng.uniform(0.02, 0.05)
            event_shock = magnitude if self._rng.random() > 0.5 else -magnitude

        results = {}
        rho = INTRA_SECTOR_CORRELATION

        for ticker, params in self._tickers.items():
            z_sector = sector_shocks[params.sector]
            z_indiv = self._rng.gauss(0, 1)
            z = rho * z_sector + math.sqrt(1 - rho ** 2) * z_indiv

            # GBM step
            drift = (params.mu - 0.5 * params.sigma ** 2) * self._dt
            diffusion = params.sigma * math.sqrt(self._dt) * z
            new_price = params.price * math.exp(drift + diffusion)

            # Apply event shock if this ticker was selected
            if ticker == event_ticker:
                new_price *= (1 + event_shock)

            # Clamp to prevent negative/zero prices
            new_price = max(new_price, 0.01)

            params.price = new_price
            results[ticker] = round(new_price, 2)

        return results
```

## Random Events

To add visual drama (prices flash, sparklines spike), the simulator occasionally generates sudden moves:

- **Frequency**: ~2% chance per tick (roughly once every 25 seconds with 500ms ticks)
- **Magnitude**: 2-5% sudden move, equally likely up or down
- **Scope**: One randomly selected ticker per event
- **Mechanism**: Multiplicative shock applied after the GBM step

This creates moments where a ticker suddenly jumps, making the watchlist feel alive and giving the user something to react to.

## Time Step Calculation

The `dt` parameter converts the tick interval into a fraction of a trading year:

```python
dt = tick_interval_seconds / (252 * 6.5 * 3600)
#    252 trading days * 6.5 hours/day * 3600 seconds/hour
```

With a 500ms tick interval: `dt = 0.5 / 5_896_800 ≈ 8.48e-8`

This keeps the per-tick volatility small and realistic. Over a simulated trading day (~46,800 ticks at 500ms), the cumulative price drift and volatility approximate real daily ranges.

## Full Simulator Implementation

```python
import asyncio
from datetime import datetime, timezone


class SimulatorMarketData(MarketDataSource):
    """Market data source that generates simulated prices using GBM."""

    def __init__(self, tick_interval: float = 0.5) -> None:
        self._tick_interval = tick_interval
        self._cache = PriceCache()
        self._engine = SimulationEngine(tick_interval=tick_interval)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def register_ticker(self, ticker: str, seed_price: float | None = None) -> None:
        self._engine.add_ticker(ticker, seed_price)

    def unregister_ticker(self, ticker: str) -> None:
        self._engine.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_latest(self, ticker: str) -> PriceUpdate | None:
        return self._cache.get(ticker)

    def get_all_latest(self) -> dict[str, PriceUpdate]:
        return self._cache.get_all()

    async def _tick_loop(self) -> None:
        while True:
            updates = self._engine.tick()
            now = datetime.now(timezone.utc)
            for ticker, price in updates.items():
                self._cache.update(ticker, price, now)
            await asyncio.sleep(self._tick_interval)
```

## Properties

| Property | Value |
|----------|-------|
| Tick interval | 500ms |
| Prices always positive | Yes (clamped to 0.01 minimum) |
| Correlated sectors | tech, finance, auto, media, other |
| Intra-sector correlation | 0.6 |
| Event frequency | ~2% per tick |
| Event magnitude | 2-5% sudden move |
| Drift (default) | 8% annualized |
| Volatility (default) | 25% annualized |
| No external dependencies | Correct — pure Python, stdlib only |

## File Structure

```
backend/
  src/
    market/
      engine.py          # SimulationEngine class (GBM math, correlation, events)
      simulator.py       # SimulatorMarketData class (MarketDataSource impl)
      seed_prices.py     # SEED_PRICES dict and TICKER_PROFILES
```
