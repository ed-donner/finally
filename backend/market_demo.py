"""FinAlly Market Data Demo.

Run with:  uv run market_demo.py

Streams live price updates using whichever data source is configured:
  - Simulator (default, no API key required)
  - Massive API (set MASSIVE_API_KEY environment variable)

Prints a compact update line for each price change, then a summary on exit.
"""

from __future__ import annotations

import asyncio
import signal
import time

from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.seed_prices import SEED_PRICES

TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]
DURATION = 30  # seconds


def _color(direction: str) -> tuple[str, str]:
    if direction == "up":
        return "\033[32m", "\033[0m"  # green
    if direction == "down":
        return "\033[31m", "\033[0m"  # red
    return "\033[90m", "\033[0m"  # dim


def _arrow(direction: str) -> str:
    return {"up": "▲", "down": "▼"}.get(direction, "─")


async def run() -> None:
    cache = PriceCache()
    source = create_market_data_source(cache)

    await source.start(TICKERS)
    start = time.time()
    last_version = -1

    print(f"\n  FinAlly Market Demo  —  streaming {len(TICKERS)} tickers for {DURATION}s\n")
    print(f"  {'TICKER':<8} {'PRICE':>10}  {'CHANGE':>8}  {'CHG%':>7}  DIR")
    print(f"  {'─' * 8}  {'─' * 9}  {'─' * 8}  {'─' * 6}  {'─' * 3}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    try:
        while not stop_event.is_set() and time.time() - start < DURATION:
            await asyncio.sleep(0.5)

            if cache.version == last_version:
                continue
            last_version = cache.version

            for ticker in TICKERS:
                u = cache.get(ticker)
                if u is None:
                    continue
                on, off = _color(u.direction)
                price_fmt = f"{u.price:>10,.2f}"
                change_fmt = f"{u.change:>+8.2f}"
                pct_fmt = f"{u.change_percent:>+6.2f}%"
                arrow = _arrow(u.direction)
                ts = time.strftime("%H:%M:%S", time.localtime(u.timestamp))
                print(f"  {on}{ticker:<8}  ${price_fmt}  {change_fmt}  {pct_fmt}  {arrow}  {ts}{off}")

    finally:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
        await source.stop()

    _print_summary(cache)


def _print_summary(cache: PriceCache) -> None:
    print(f"\n  {'─' * 60}")
    print("  Session Summary\n")
    print(f"  {'TICKER':<8}  {'SEED':>10}  {'FINAL':>10}  {'SESSION':>9}")
    print(f"  {'─' * 8}  {'─' * 10}  {'─' * 10}  {'─' * 9}")

    for ticker in TICKERS:
        u = cache.get(ticker)
        seed = SEED_PRICES.get(ticker)
        if u is None or seed is None:
            continue
        pct = (u.price - seed) / seed * 100
        on, off = _color("up" if pct > 0 else "down" if pct < 0 else "flat")
        print(
            f"  {ticker:<8}  ${seed:>9,.2f}  "
            f"{on}${u.price:>9,.2f}  {pct:>+8.2f}%{off}"
        )

    print()


if __name__ == "__main__":
    asyncio.run(run())
