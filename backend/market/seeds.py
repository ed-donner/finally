from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TickerSeed:
    price: float     # realistic starting price
    mu: float        # annual drift (expected return)
    sigma: float     # annual volatility (tech > financials)
    sector: str      # correlation grouping


SEEDS: dict[str, TickerSeed] = {
    "AAPL":  TickerSeed(190.0, 0.08, 0.28, "tech"),
    "GOOGL": TickerSeed(175.0, 0.10, 0.30, "tech"),
    "MSFT":  TickerSeed(420.0, 0.09, 0.26, "tech"),
    "AMZN":  TickerSeed(185.0, 0.11, 0.33, "tech"),
    "TSLA":  TickerSeed(250.0, 0.05, 0.55, "tech"),
    "NVDA":  TickerSeed(880.0, 0.15, 0.50, "tech"),
    "META":  TickerSeed(500.0, 0.10, 0.35, "tech"),
    "JPM":   TickerSeed(200.0, 0.06, 0.20, "financial"),
    "V":     TickerSeed(275.0, 0.07, 0.19, "financial"),
    "NFLX":  TickerSeed(630.0, 0.09, 0.40, "tech"),
}

DEFAULT_SEED = TickerSeed(100.0, 0.07, 0.30, "other")
