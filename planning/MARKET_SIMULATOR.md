# Market Simulator — Design and Implementation

## Purpose

The simulator generates synthetic but realistic-looking stock price data when no Massive API key is set. It implements the same `MarketDataSource` interface as `MassiveClient`, so all downstream code is completely unaware of which source is active.

---

## Mathematical Model

### Geometric Brownian Motion (GBM)

Each ticker price evolves according to discrete GBM:

```
S(t + Δt) = S(t) · exp((μ - σ²/2)·Δt + σ·√Δt · Z)
```

Where:
- `S(t)` — current price
- `μ` — drift per second (annualised drift / seconds_per_year)
- `σ` — volatility per second (annualised vol / √seconds_per_year)
- `Δt` — time step in seconds (0.5 for 500ms updates)
- `Z` — standard normal random variable `~ N(0, 1)`

GBM is the standard model for equity prices. It guarantees prices stay positive and produces the log-normal return distributions seen in real markets. The drift term produces a slight upward bias (simulating a bull market), and the volatility term produces the random fluctuations.

### Correlated Moves

Real stocks don't move independently — tech stocks tend to move together, and the whole market often moves in the same direction. We model this using Cholesky decomposition of a correlation matrix.

Instead of drawing independent `Z` values per ticker, we draw a correlated multivariate normal vector:

```python
import numpy as np

# L = lower-triangular Cholesky factor of correlation matrix C
# such that L @ L.T = C
L = np.linalg.cholesky(correlation_matrix)

# Draw uncorrelated normals
z_uncorrelated = np.random.standard_normal(n_tickers)

# Produce correlated normals
z_correlated = L @ z_uncorrelated
```

Each element of `z_correlated` is then used as the `Z` in the GBM formula for its respective ticker.

### Random Events (Shocks)

To create drama and demonstrate price flashes, occasional larger moves are injected:

- With probability `p_event = 0.002` per tick per ticker (~once every ~4 minutes for a single ticker), a shock is applied.
- Shock magnitude: `U(-0.05, +0.05)` — a random move of up to ±5%.
- After a shock, the drift reverts to 0 for the next 10 ticks (mean reversion).

---

## Seed Prices and Parameters

Default parameters for the 10 watchlist tickers. Values are designed to look realistic as of 2025.

```python
TICKER_PARAMS = {
    #           seed_price  annual_drift  annual_vol
    "AAPL":  (  190.00,     0.15,         0.25),
    "GOOGL": (  175.00,     0.12,         0.28),
    "MSFT":  (  415.00,     0.18,         0.22),
    "AMZN":  (  185.00,     0.20,         0.30),
    "TSLA":  (  250.00,     0.08,         0.55),  # higher vol
    "NVDA":  (  875.00,     0.35,         0.50),  # higher growth + vol
    "META":  (  520.00,     0.25,         0.32),
    "JPM":   (  200.00,     0.10,         0.20),  # lower vol (financials)
    "V":     (  270.00,     0.12,         0.18),  # lower vol (payments)
    "NFLX":  (  700.00,     0.15,         0.38),
}
```

Annual drift and volatility are converted to per-tick values at startup:

```python
SECONDS_PER_YEAR = 252 * 6.5 * 3600  # trading seconds per year

dt = 0.5  # seconds per tick

drift_per_tick = (annual_drift - 0.5 * annual_vol**2) * (dt / SECONDS_PER_YEAR)
vol_per_tick   = annual_vol * (dt / SECONDS_PER_YEAR) ** 0.5
```

### Correlation Matrix

A simplified block structure reflecting real-world sector correlations:

```
         AAPL  GOOGL  MSFT  AMZN  TSLA  NVDA  META  JPM   V     NFLX
AAPL  [  1.00  0.65   0.70  0.55  0.45  0.60  0.60  0.30  0.35  0.50 ]
GOOGL [  0.65  1.00   0.65  0.60  0.40  0.55  0.65  0.25  0.30  0.55 ]
MSFT  [  0.70  0.65   1.00  0.55  0.42  0.60  0.58  0.30  0.32  0.48 ]
AMZN  [  0.55  0.60   0.55  1.00  0.40  0.50  0.60  0.25  0.35  0.60 ]
TSLA  [  0.45  0.40   0.42  0.40  1.00  0.55  0.38  0.20  0.22  0.40 ]
NVDA  [  0.60  0.55   0.60  0.50  0.55  1.00  0.52  0.25  0.28  0.45 ]
META  [  0.60  0.65   0.58  0.60  0.38  0.52  1.00  0.25  0.30  0.58 ]
JPM   [  0.30  0.25   0.30  0.25  0.20  0.25  0.25  1.00  0.65  0.22 ]
V     [  0.35  0.30   0.32  0.35  0.22  0.28  0.30  0.65  1.00  0.28 ]
NFLX  [  0.50  0.55   0.48  0.60  0.40  0.45  0.58  0.22  0.28  1.00 ]
```

Note: JPM and V are highly correlated with each other (financials/payments) but less correlated with tech. TSLA and NVDA are moderately correlated (both high-vol, tech-adjacent).

---

## Implementation

Full implementation in `backend/app/market/simulator.py`:

```python
import asyncio
import logging
import numpy as np
from datetime import datetime, timezone
from typing import NamedTuple

from .interface import MarketDataSource
from .cache import PriceCache
from .models import PriceUpdate, DailyBar

logger = logging.getLogger(__name__)

SECONDS_PER_YEAR = 252 * 6.5 * 3600
TICK_INTERVAL = 0.5  # seconds between price updates

# (seed_price, annual_drift, annual_volatility)
TICKER_PARAMS: dict[str, tuple[float, float, float]] = {
    "AAPL":  (190.00, 0.15, 0.25),
    "GOOGL": (175.00, 0.12, 0.28),
    "MSFT":  (415.00, 0.18, 0.22),
    "AMZN":  (185.00, 0.20, 0.30),
    "TSLA":  (250.00, 0.08, 0.55),
    "NVDA":  (875.00, 0.35, 0.50),
    "META":  (520.00, 0.25, 0.32),
    "JPM":   (200.00, 0.10, 0.20),
    "V":     (270.00, 0.12, 0.18),
    "NFLX":  (700.00, 0.15, 0.38),
}

CORRELATION_MATRIX = np.array([
    # AAPL  GOOGL  MSFT   AMZN   TSLA   NVDA   META   JPM    V      NFLX
    [1.00,  0.65,  0.70,  0.55,  0.45,  0.60,  0.60,  0.30,  0.35,  0.50],
    [0.65,  1.00,  0.65,  0.60,  0.40,  0.55,  0.65,  0.25,  0.30,  0.55],
    [0.70,  0.65,  1.00,  0.55,  0.42,  0.60,  0.58,  0.30,  0.32,  0.48],
    [0.55,  0.60,  0.55,  1.00,  0.40,  0.50,  0.60,  0.25,  0.35,  0.60],
    [0.45,  0.40,  0.42,  0.40,  1.00,  0.55,  0.38,  0.20,  0.22,  0.40],
    [0.60,  0.55,  0.60,  0.50,  0.55,  1.00,  0.52,  0.25,  0.28,  0.45],
    [0.60,  0.65,  0.58,  0.60,  0.38,  0.52,  1.00,  0.25,  0.30,  0.58],
    [0.30,  0.25,  0.30,  0.25,  0.20,  0.25,  0.25,  1.00,  0.65,  0.22],
    [0.35,  0.30,  0.32,  0.35,  0.22,  0.28,  0.30,  0.65,  1.00,  0.28],
    [0.50,  0.55,  0.48,  0.60,  0.40,  0.45,  0.58,  0.22,  0.28,  1.00],
])

P_EVENT = 0.002       # probability of shock event per ticker per tick
EVENT_REVERT_TICKS = 10  # ticks of reduced drift after a shock


class _TickerState(NamedTuple):
    price: float
    drift: float    # per-tick drift (already computed)
    vol: float      # per-tick volatility (already computed)
    revert: int     # ticks remaining in post-shock mean reversion


class MarketSimulator(MarketDataSource):
    """Generates synthetic stock prices using correlated Geometric Brownian Motion.

    Prices for the 10 default tickers use a shared correlation matrix.
    Additional tickers added at runtime are simulated independently with
    default drift/vol parameters derived from a reasonable baseline.
    """

    def __init__(self, cache: PriceCache) -> None:
        self._cache = cache
        self._tickers: list[str] = []
        self._states: dict[str, _TickerState] = {}
        self._cholesky: np.ndarray | None = None  # pre-computed for default tickers
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # MarketDataSource interface
    # ------------------------------------------------------------------

    async def start(self, tickers: list[str]) -> None:
        self._init_tickers(tickers)
        self._task = asyncio.create_task(self._tick_loop())
        logger.info("MarketSimulator started with %d tickers", len(self._tickers))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MarketSimulator stopped")

    def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper()
        if ticker not in self._states:
            self._states[ticker] = self._make_state(ticker)
            self._tickers.append(ticker)
            self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper()
        if ticker in self._states:
            del self._states[ticker]
            self._tickers = [t for t in self._tickers if t != ticker]
            self._rebuild_cholesky()

    async def get_daily_bars(self, ticker: str, from_date: str, to_date: str) -> list[DailyBar]:
        # Simulator has no historical data; return empty list
        return []

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _make_state(self, ticker: str) -> _TickerState:
        seed_price, annual_drift, annual_vol = TICKER_PARAMS.get(
            ticker, (100.0, 0.12, 0.30)  # sensible defaults for unknown tickers
        )
        drift_per_tick = (annual_drift - 0.5 * annual_vol**2) * (TICK_INTERVAL / SECONDS_PER_YEAR)
        vol_per_tick   = annual_vol * (TICK_INTERVAL / SECONDS_PER_YEAR) ** 0.5
        return _TickerState(price=seed_price, drift=drift_per_tick, vol=vol_per_tick, revert=0)

    def _init_tickers(self, tickers: list[str]) -> None:
        self._tickers = [t.upper() for t in tickers]
        for ticker in self._tickers:
            self._states[ticker] = self._make_state(ticker)
        self._rebuild_cholesky()

    def _rebuild_cholesky(self) -> None:
        n = len(self._tickers)
        if n == 0:
            self._cholesky = None
            return

        # Build correlation matrix for current ticker set.
        # For tickers in the default set, use the pre-defined correlations.
        # For unknown tickers, assume correlation 0.3 with all others.
        default_tickers = list(TICKER_PARAMS.keys())
        default_idx = {t: i for i, t in enumerate(default_tickers)}

        C = np.eye(n)
        for i, ti in enumerate(self._tickers):
            for j, tj in enumerate(self._tickers):
                if i == j:
                    continue
                if ti in default_idx and tj in default_idx:
                    C[i, j] = CORRELATION_MATRIX[default_idx[ti], default_idx[tj]]
                else:
                    C[i, j] = 0.3  # default cross-asset correlation

        self._cholesky = np.linalg.cholesky(C)

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_INTERVAL)
            updates = self._compute_tick()
            if updates:
                await self._cache.update(updates)

    def _compute_tick(self) -> list[PriceUpdate]:
        n = len(self._tickers)
        if n == 0 or self._cholesky is None:
            return []

        # Draw correlated normal random variables
        z_raw = np.random.standard_normal(n)
        z = self._cholesky @ z_raw  # shape (n,)

        now = datetime.now(tz=timezone.utc)
        updates = []
        new_states = {}

        for i, ticker in enumerate(self._tickers):
            state = self._states[ticker]

            # Check for random shock event
            drift = state.drift
            if state.revert > 0:
                drift = 0.0  # suppress drift during reversion
                revert = state.revert - 1
            elif np.random.random() < P_EVENT:
                shock_pct = np.random.uniform(-0.05, 0.05)
                prev_price = state.price
                shocked_price = max(prev_price * (1 + shock_pct), 0.01)
                revert = EVENT_REVERT_TICKS
                update = PriceUpdate(
                    ticker=ticker,
                    price=round(shocked_price, 4),
                    prev_price=round(prev_price, 4),
                    timestamp=now,
                    change=round(shocked_price - prev_price, 4),
                    change_pct=round(shock_pct * 100, 4),
                )
                updates.append(update)
                new_states[ticker] = _TickerState(
                    price=shocked_price,
                    drift=state.drift,
                    vol=state.vol,
                    revert=revert,
                )
                continue  # skip GBM for this tick; shock already applied
            else:
                revert = 0

            # GBM update: S(t+dt) = S(t) * exp(drift + vol * Z)
            log_return = drift + state.vol * z[i]
            new_price = max(state.price * np.exp(log_return), 0.01)
            prev_price = state.price
            change = new_price - prev_price
            change_pct = (change / prev_price * 100) if prev_price else 0.0

            updates.append(PriceUpdate(
                ticker=ticker,
                price=round(new_price, 4),
                prev_price=round(prev_price, 4),
                timestamp=now,
                change=round(change, 4),
                change_pct=round(change_pct, 4),
            ))
            new_states[ticker] = _TickerState(
                price=new_price,
                drift=state.drift,
                vol=state.vol,
                revert=revert,
            )

        # Commit state updates
        for ticker, state in new_states.items():
            self._states[ticker] = state

        return updates
```

---

## Behavior Characteristics

| Property | Value |
|---|---|
| Update frequency | Every 500ms |
| Typical price change per tick | 0.01%–0.05% (within 1 tick) |
| Typical daily range | 1%–3% for stable stocks; 3%–8% for high-vol tickers |
| Shock probability | ~0.2% per tick per ticker (~once per 4 min per ticker) |
| Shock magnitude | ±0–5% instantaneous move |
| Post-shock behavior | Drift suppressed for 5 seconds (10 ticks) |
| Correlation | Moderate (0.45–0.70) within tech, low (0.20–0.35) across sectors |
| Prices | Always positive (GBM property; also explicitly clamped to 0.01) |

---

## Testing the Simulator

```python
import asyncio
from backend.app.market.cache import PriceCache
from backend.app.market.simulator import MarketSimulator

async def demo():
    cache = PriceCache()
    sim = MarketSimulator(cache)
    await sim.start(["AAPL", "MSFT", "TSLA"])

    for _ in range(10):
        await asyncio.sleep(0.5)
        prices = cache.get_all()
        for ticker, p in prices.items():
            print(f"{ticker}: ${p.price:.2f}  ({p.change_pct:+.3f}%)")
        print("---")

    await sim.stop()

asyncio.run(demo())
```

---

## Known Limitations

- **No market hours** — prices update 24/7. In production, a more realistic simulator would pause overnight and on weekends. For this demo, continuous updates are fine.
- **No daily open** — each session inherits the previous tick's close as "open". The `todaysChangePerc` from the real API (vs. previous close) has no direct equivalent; we use intraday change vs. the price at simulator start.
- **No historical data** — `get_daily_bars()` returns an empty list. Frontend sparklines accumulate from SSE since page load; the main chart area has data only for the current session.
- **Unbounded random walk** — over very long sessions (hours), prices may drift significantly from their seeds. This is inherent to GBM. For a short demo session it's acceptable.
- **No order book** — the simulator produces mid prices. Bid/ask spread is not modeled.

---

## Extension Points

If a more sophisticated simulator is needed:

- **Mean reversion**: Add an Ornstein-Uhlenbeck process to pull prices back toward a long-run mean — prevents extreme drift in long sessions.
- **Intraday patterns**: Scale volatility higher near open/close and lower mid-day to mimic the real U-shaped intraday vol curve.
- **Regime switching**: A two-state Markov chain (bull/bear) changes drift sign, producing sustained trends.
- **Corporate events**: Pre-scheduled price shocks on specific tickers at specific times (e.g., simulate an "earnings beat" at T+5min).
