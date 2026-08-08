from __future__ import annotations

import math
import random

from .seeds import DEFAULT_SEED, SEEDS, TickerSeed
from .types import Bar

SECONDS_PER_YEAR = 252 * 6.5 * 3600      # 252 trading days x 6.5h
DT = 0.5 / SECONDS_PER_YEAR              # one 500ms step as a fraction of a year
DT_DAY = 1.0 / 252                       # one trading day, for synthetic history
EVENT_PROBABILITY = 0.005               # ~0.5% chance of a shock per ticker/step

# Correlation weights: market + sector + idiosyncratic ~= unit variance.
W_MARKET, W_SECTOR = 0.5, 0.4
W_IDIO = math.sqrt(max(0.0, 1 - W_MARKET**2 - W_SECTOR**2))


class SimEngine:
    """Advances correlated GBM prices one 500ms step at a time."""

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._prices: dict[str, float] = {}
        self._seeds: dict[str, TickerSeed] = {}

    # -- seeding -----------------------------------------------------------

    def _ensure(self, ticker: str) -> None:
        if ticker not in self._prices:
            base = SEEDS.get(ticker)
            if base is None:
                jitter = self._rng.uniform(0.5, 2.0)
                base = TickerSeed(
                    DEFAULT_SEED.price * jitter,
                    DEFAULT_SEED.mu, DEFAULT_SEED.sigma, DEFAULT_SEED.sector,
                )
            self._seeds[ticker] = base
            self._prices[ticker] = base.price

    # -- live stepping -----------------------------------------------------

    def step(self, tickers: list[str]) -> dict[str, float]:
        for t in tickers:
            self._ensure(t)

        z_market = self._rng.gauss(0, 1)
        sector_factors: dict[str, float] = {}

        out: dict[str, float] = {}
        for t in tickers:
            seed = self._seeds[t]
            if seed.sector not in sector_factors:
                sector_factors[seed.sector] = self._rng.gauss(0, 1)
            z = (W_MARKET * z_market
                 + W_SECTOR * sector_factors[seed.sector]
                 + W_IDIO * self._rng.gauss(0, 1))

            drift = (seed.mu - 0.5 * seed.sigma**2) * DT
            diffusion = seed.sigma * math.sqrt(DT) * z
            price = self._prices[t] * math.exp(drift + diffusion)

            if self._rng.random() < EVENT_PROBABILITY:
                shock = self._rng.uniform(0.02, 0.05)
                price *= (1 + shock) if self._rng.random() < 0.5 else (1 - shock)

            price = round(max(price, 0.01), 2)
            self._prices[t] = price
            out[t] = price
        return out

    # -- synthetic history -------------------------------------------------

    def history(self, ticker: str, days: int, end_ms: int) -> list[Bar]:
        """Deterministic daily OHLC ending near the ticker's current price.

        Walks GBM *backwards* from the live price so the last bar's close lines
        up with what the stream is currently showing. Uses a per-ticker RNG so a
        given symbol always renders the same past (stable across chart reopens).
        """
        self._ensure(ticker)
        seed = self._seeds[ticker]
        rng = random.Random(hash((ticker, days)) & 0xFFFFFFFF)

        close = self._prices[ticker]
        rows: list[tuple[float, float, float, float]] = []  # o,h,l,c per day
        for _ in range(days):
            z = rng.gauss(0, 1)
            drift = (seed.mu - 0.5 * seed.sigma**2) * DT_DAY
            diffusion = seed.sigma * math.sqrt(DT_DAY) * z
            prev_close = close / math.exp(drift + diffusion)  # step backward
            o, c = prev_close, close
            hi = max(o, c) * (1 + abs(rng.gauss(0, 0.4)) * seed.sigma * math.sqrt(DT_DAY))
            lo = min(o, c) * (1 - abs(rng.gauss(0, 0.4)) * seed.sigma * math.sqrt(DT_DAY))
            rows.append((round(o, 2), round(hi, 2), round(lo, 2), round(c, 2)))
            close = prev_close

        rows.reverse()  # oldest first
        ms_per_day = 86_400_000
        return [
            Bar(t=end_ms - (days - 1 - i) * ms_per_day,
                o=o, h=h, low=low, c=c,
                v=round(rng.uniform(1e6, 5e7)))
            for i, (o, h, low, c) in enumerate(rows)
        ]
