# Market Simulator Design

The simulator generates realistic-looking stock prices when no Massive API key is configured. It implements the `MarketDataSource` interface defined in [MARKET_INTERFACE.md](./MARKET_INTERFACE.md).

---

## Goals

- Produce prices that look and feel like real market data
- Update all tracked tickers every ~500ms
- Include correlated moves across related tickers
- Generate occasional dramatic events for visual interest
- Require zero external dependencies or API keys
- Be deterministic enough for testing when seeded, but random enough for demo purposes

---

## Algorithm: Geometric Brownian Motion (GBM)

Each ticker's price follows a discrete-time GBM step:

```
dS = S * (μ * dt + σ * √dt * Z)
```

Where:

- `S` = current price
- `μ` = drift (annualized, converted to per-tick)
- `σ` = volatility (annualized, converted to per-tick)
- `dt` = time step as fraction of a year (~0.5s / seconds_in_trading_year)
- `Z` = standard normal random variable

In code, each tick:

```python
new_price = price * (1 + drift * dt + volatility * sqrt(dt) * z)
```

This produces realistic log-normal price distributions with controllable drift and volatility.

---

## Seed Prices and Parameters

Each ticker has a starting price and per-ticker volatility to reflect real market behavior:

```python
# backend/market/simulator.py

TICKER_SEEDS: dict[str, dict] = {
    "AAPL":  {"price": 190.00, "volatility": 0.25, "sector": "tech"},
    "GOOGL": {"price": 175.00, "volatility": 0.28, "sector": "tech"},
    "MSFT":  {"price": 420.00, "volatility": 0.22, "sector": "tech"},
    "AMZN":  {"price": 185.00, "volatility": 0.30, "sector": "tech"},
    "TSLA":  {"price": 250.00, "volatility": 0.55, "sector": "auto"},
    "NVDA":  {"price": 880.00, "volatility": 0.45, "sector": "tech"},
    "META":  {"price": 500.00, "volatility": 0.32, "sector": "tech"},
    "JPM":   {"price": 195.00, "volatility": 0.20, "sector": "finance"},
    "V":     {"price": 280.00, "volatility": 0.18, "sector": "finance"},
    "NFLX":  {"price": 620.00, "volatility": 0.35, "sector": "tech"},
}

# Default for unknown tickers
DEFAULT_SEED = {"price": 100.00, "volatility": 0.30, "sector": "other"}

# Shared parameters
DRIFT = 0.0       # Zero drift — no systematic upward/downward bias
TICK_INTERVAL = 0.5  # Seconds between updates
SECONDS_PER_TRADING_YEAR = 252 * 6.5 * 3600  # ~5.9M seconds
```

**Why zero drift?** Over the short session durations typical of a demo, any non-zero drift would create a visible trend bias. Zero drift keeps prices meandering realistically around their starting point.

---

## Correlated Moves

Tickers in the same sector should move together. This is achieved using a **sector factor model**:

1. Each tick, generate one random factor `Z_sector` per sector
2. Each ticker's random component is a weighted blend of the sector factor and an idiosyncratic factor:

```python
Z_ticker = correlation * Z_sector + sqrt(1 - correlation^2) * Z_idio
```

Where `correlation` controls how strongly tickers co-move within a sector (e.g., 0.6 for tech, 0.5 for finance).

```python
SECTOR_CORRELATION = {
    "tech": 0.6,
    "finance": 0.5,
    "auto": 0.3,
    "other": 0.0,  # Uncorrelated
}
```

This means when AAPL jumps up, GOOGL and MSFT are likely (but not certain) to also move up on the same tick.

---

## Random Events

To create visual drama (big green/red flashes), the simulator occasionally triggers sudden moves:

```python
EVENT_PROBABILITY = 0.002  # ~0.2% chance per tick per ticker
EVENT_MAGNITUDE_MIN = 0.02  # 2% minimum move
EVENT_MAGNITUDE_MAX = 0.05  # 5% maximum move
```

When an event triggers:

1. Skip the normal GBM step for that ticker
2. Apply a sudden move: `price *= (1 + uniform(0.02, 0.05) * random_sign)`
3. This creates the dramatic flashes that make the demo visually exciting

---

## Implementation

```python
# backend/market/simulator.py

import asyncio
import math
import random
from datetime import datetime, timezone
from .base import MarketDataSource, PriceTick


class SimulatorMarketDataSource(MarketDataSource):

    def __init__(self):
        self._tickers: dict[str, _TickerState] = {}
        self._callback = None
        self._task: asyncio.Task | None = None

    def set_price_callback(self, callback) -> None:
        self._callback = callback

    async def start(self, tickers: list[str]) -> None:
        for ticker in tickers:
            self._init_ticker(ticker)
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self._tickers:
            self._init_ticker(ticker)
            # Emit an initial price immediately
            state = self._tickers[ticker]
            tick = PriceTick(
                ticker=ticker,
                price=state.price,
                prev_close=state.prev_close,
                timestamp=datetime.now(timezone.utc),
            )
            if self._callback:
                await self._callback(tick)

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers.pop(ticker, None)

    async def get_price_now(self, ticker: str) -> PriceTick | None:
        """Return current price or generate one for a new ticker."""
        if ticker not in self._tickers:
            self._init_ticker(ticker)
        state = self._tickers[ticker]
        return PriceTick(
            ticker=ticker,
            price=state.price,
            prev_close=state.prev_close,
            timestamp=datetime.now(timezone.utc),
        )

    def _init_ticker(self, ticker: str) -> None:
        """Initialize a ticker with seed data."""
        seed = TICKER_SEEDS.get(ticker, DEFAULT_SEED)
        self._tickers[ticker] = _TickerState(
            price=seed["price"],
            prev_close=seed["price"],  # prev_close = seed price at start
            volatility=seed["volatility"],
            sector=seed["sector"],
        )

    async def _run_loop(self) -> None:
        """Main simulation loop — updates all tickers every TICK_INTERVAL."""
        dt = TICK_INTERVAL / SECONDS_PER_TRADING_YEAR

        while True:
            await asyncio.sleep(TICK_INTERVAL)
            now = datetime.now(timezone.utc)

            # Generate sector factors
            sector_factors = {
                sector: random.gauss(0, 1)
                for sector in SECTOR_CORRELATION
            }

            for ticker, state in list(self._tickers.items()):
                # Check for random event
                if random.random() < EVENT_PROBABILITY:
                    magnitude = random.uniform(
                        EVENT_MAGNITUDE_MIN, EVENT_MAGNITUDE_MAX
                    )
                    sign = random.choice([-1, 1])
                    state.price *= (1 + magnitude * sign)
                else:
                    # Normal GBM step with sector correlation
                    corr = SECTOR_CORRELATION.get(state.sector, 0.0)
                    z_sector = sector_factors.get(state.sector, 0.0)
                    z_idio = random.gauss(0, 1)
                    z = corr * z_sector + math.sqrt(1 - corr**2) * z_idio

                    state.price *= (1 + DRIFT * dt + state.volatility * math.sqrt(dt) * z)

                # Clamp to prevent negative/zero prices
                state.price = max(state.price, 0.01)

                tick = PriceTick(
                    ticker=ticker,
                    price=round(state.price, 2),
                    prev_close=state.prev_close,
                    timestamp=now,
                )
                if self._callback:
                    await self._callback(tick)


class _TickerState:
    """Mutable state for a single simulated ticker."""
    __slots__ = ("price", "prev_close", "volatility", "sector")

    def __init__(self, price: float, prev_close: float, volatility: float, sector: str):
        self.price = price
        self.prev_close = prev_close
        self.volatility = volatility
        self.sector = sector
```

---

## Key Design Decisions

### prev_close is fixed at the seed price

The simulator records each ticker's seed price as its `prev_close`. This value is fixed for the lifetime of the process — it does not roll over at any time boundary. This is a deliberate simplification: the simulator has no concept of "market open" or "market close." `day_change` and `day_change_pct` in the SSE stream represent change since process start. On container restart, the simulator resets to seed prices and `prev_close` resets with it.

### No market hours

The simulator runs 24/7. There is no concept of market open/close, pre-market, or after-hours. Prices update continuously at the configured tick interval.

### Prices are rounded to cents

All prices emitted by the simulator are rounded to 2 decimal places (`round(price, 2)`) to match real stock price display conventions.

### Unknown tickers default to $100

When a ticker not in the seed table is added (e.g., the AI trades "PYPL"), it starts at $100 with 0.30 volatility and no sector correlation. This ensures "trade any ticker" works without requiring a predefined list.

### No persistence

The simulator is purely in-memory. All state resets on process restart. This is fine because:

- The database stores trade history and portfolio state
- Sparkline charts accumulate from the SSE stream on the frontend
- The P&L chart uses portfolio snapshots from the database
- Price history is not needed — only the current price matters

---

## Testing

For deterministic testing, the simulator can accept a `random.Random` instance with a fixed seed:

```python
class SimulatorMarketDataSource(MarketDataSource):
    def __init__(self, rng: random.Random | None = None):
        self._rng = rng or random.Random()
        # ... use self._rng.gauss(), self._rng.random(), etc.
```

This allows tests to produce repeatable price sequences while production uses the default global RNG.

---

## Performance

With 10-25 tickers updating every 500ms, the simulator does ~20-50 floating point operations per tick. This is trivially fast — no optimization needed. The `asyncio.sleep(0.5)` dominates the timing. The entire tick cycle for all tickers completes in microseconds.
