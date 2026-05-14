import asyncio
import logging
import numpy as np
from datetime import datetime, timezone
from typing import NamedTuple

from .interface import MarketDataSource
from .cache import PriceCache
from .models import PriceUpdate, DailyBar

logger = logging.getLogger(__name__)

SECONDS_PER_YEAR = 252 * 6.5 * 3600  # trading seconds per year
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

# Correlation matrix for the 10 default tickers (rows/cols ordered as TICKER_PARAMS keys)
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

P_EVENT = 0.002          # probability of shock event per ticker per tick
EVENT_REVERT_TICKS = 10  # ticks of reduced drift after a shock


class _TickerState(NamedTuple):
    price: float
    drift: float   # per-tick drift (already computed from annual params)
    vol: float     # per-tick volatility (already computed)
    revert: int    # ticks remaining in post-shock mean reversion


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
        self._cholesky: np.ndarray | None = None
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
        # Simulator has no historical data
        return []

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _make_state(self, ticker: str) -> _TickerState:
        seed_price, annual_drift, annual_vol = TICKER_PARAMS.get(
            ticker, (100.0, 0.12, 0.30)  # sensible defaults for unknown tickers
        )
        drift_per_tick = (annual_drift - 0.5 * annual_vol ** 2) * (TICK_INTERVAL / SECONDS_PER_YEAR)
        vol_per_tick = annual_vol * (TICK_INTERVAL / SECONDS_PER_YEAR) ** 0.5
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
                    C[i, j] = 0.3  # default cross-asset correlation for unknown tickers

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

        z_raw = np.random.standard_normal(n)
        z = self._cholesky @ z_raw  # correlated normal random variables

        now = datetime.now(tz=timezone.utc)
        updates = []
        new_states = {}

        for i, ticker in enumerate(self._tickers):
            state = self._states[ticker]

            # Check for random shock event
            if state.revert > 0:
                drift = 0.0  # suppress drift during mean reversion period
                revert = state.revert - 1
            elif np.random.random() < P_EVENT:
                # Apply an instantaneous shock of up to ±5%
                shock_pct = np.random.uniform(-0.05, 0.05)
                prev_price = state.price
                shocked_price = max(prev_price * (1 + shock_pct), 0.01)
                revert = EVENT_REVERT_TICKS
                updates.append(PriceUpdate(
                    ticker=ticker,
                    price=round(shocked_price, 4),
                    prev_price=round(prev_price, 4),
                    timestamp=now,
                    change=round(shocked_price - prev_price, 4),
                    change_pct=round(shock_pct * 100, 4),
                ))
                new_states[ticker] = _TickerState(
                    price=shocked_price,
                    drift=state.drift,
                    vol=state.vol,
                    revert=revert,
                )
                continue  # skip GBM for this tick; shock already applied
            else:
                drift = state.drift
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

        for ticker, state in new_states.items():
            self._states[ticker] = state

        return updates
