"""FinAlly Market Data Demo.

Run with:  uv run --extra demo market_data_demo.py

A live terminal dashboard for the market-data subsystem. Compared to the
original demo this version exercises the *current* backend API:

  - ``create_market_data_source`` — auto-selects the GBM simulator or the
    Massive (Polygon.io) feed from ``MASSIVE_API_KEY`` (shown in the header).
  - ``PriceUpdate.day_change_percent`` — the watchlist's "daily change %"
    (vs ``day_open``), shown alongside the per-tick flash change.
  - ``source.health()`` — feed liveness (healthy dot + age of last update).
  - ``PriceCache.snapshot()`` — the same atomic (version, prices) read the SSE
    endpoint uses for change detection.
  - Dynamic watchlist + ``normalize_ticker`` — mid-session it adds a ticker
    typed in lowercase ("pypl" → PYPL) and removes one, live.

Runs ~60s or until Ctrl+C, then prints a session summary.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from app.market import PriceCache, create_market_data_source
from app.market.interface import normalize_ticker
from app.market.seed_prices import SEED_PRICES

# Sparkline characters, low to high
SPARK_CHARS = "▁▂▃▄▅▆▇█"

# Default watchlist (initial set).
INITIAL_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]

DURATION = 60  # seconds

# Scripted watchlist actions: (elapsed_seconds, action, raw_ticker).
# The raw tickers are deliberately messy to show normalization in action.
WATCHLIST_SCRIPT = [
    (15.0, "add", "  pypl "),  # normalizes to PYPL
    (30.0, "remove", "v"),     # normalizes to V
]


def sparkline(values: list[float]) -> str:
    """Render a sequence of values as a unicode sparkline."""
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    spread = hi - lo
    if spread == 0:
        return SPARK_CHARS[3] * len(values)
    n = len(SPARK_CHARS) - 1
    return "".join(SPARK_CHARS[int((v - lo) / spread * n)] for v in values)


def format_price(price: float) -> str:
    """Format a price with comma separator."""
    if price >= 1000:
        return f"{price:,.2f}"
    return f"{price:.2f}"


def _direction_style(direction: str) -> tuple[str, str]:
    """Map a tick direction to a (color, arrow-markup) pair."""
    if direction == "up":
        return "green", "[bold green]▲[/]"
    if direction == "down":
        return "red", "[bold red]▼[/]"
    return "bright_black", "[bright_black]─[/]"


def build_table(tickers: list[str], prices: dict, history: dict[str, deque]) -> Table:
    """Build the live price table from a cache snapshot."""
    table = Table(
        expand=True,
        border_style="bright_black",
        header_style="bold bright_white",
        pad_edge=True,
        padding=(0, 1),
    )
    table.add_column("Ticker", style="bold bright_white", width=8)
    table.add_column("Price", justify="right", width=11)
    table.add_column("Tick", justify="right", width=8)  # per-tick flash change
    table.add_column("", width=3)  # arrow
    table.add_column("Day %", justify="right", width=9)  # vs day_open (watchlist metric)
    table.add_column("Sparkline", width=40, no_wrap=True)

    for ticker in tickers:
        update = prices.get(ticker)
        if update is None:
            table.add_row(ticker, "---", "---", "", "---", "")
            continue

        color, arrow = _direction_style(update.direction)
        price_str = f"[{color}]${format_price(update.price)}[/]"
        tick_str = f"[{color}]{update.change:+.2f}[/]"

        # Daily change is measured against day_open, independent of the tick.
        day_color = "green" if update.day_change_percent > 0 else (
            "red" if update.day_change_percent < 0 else "bright_black"
        )
        day_str = f"[{day_color}]{update.day_change_percent:+.2f}%[/]"

        vals = list(history.get(ticker, []))
        spark_str = f"[bright_cyan]{sparkline(vals)}[/]" if len(vals) > 1 else ""

        table.add_row(ticker, price_str, tick_str, arrow, day_str, spark_str)

    return table


def build_event_log(events: deque) -> Panel:
    """Build the event log panel (shocks + watchlist actions)."""
    text = Text()
    for evt in events:
        text.append_text(Text.from_markup(evt))
        text.append("\n")
    if not events:
        text.append(
            "Watching for notable moves (>1% tick) and watchlist changes...",
            style="bright_black italic",
        )
    return Panel(
        text,
        title="[bold bright_yellow]Recent Events[/]",
        border_style="bright_black",
        height=9,
    )


def _health_markup(health: dict) -> Text:
    """Render the source health as a colored dot + freshness."""
    healthy = health.get("healthy")
    last_update = health.get("last_update")
    if not healthy:
        return Text.assemble(("●", "bold red"), (" unhealthy", "red"))
    if last_update is None:
        return Text.assemble(("●", "bold yellow"), (" starting", "yellow"))
    age = time.time() - last_update
    if age <= 2.0:
        return Text.assemble(("●", "bold green"), (f" live ({age:.1f}s)", "green"))
    return Text.assemble(("●", "bold yellow"), (f" stale ({age:.0f}s)", "yellow"))


def build_dashboard(
    tickers: list[str],
    prices: dict,
    history: dict[str, deque],
    events: deque,
    health: dict,
    source_name: str,
    start_time: float,
) -> Layout:
    """Build the full dashboard layout."""
    elapsed = time.time() - start_time
    remaining = max(0, DURATION - elapsed)

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=9),
    )

    header_text = Text.assemble(
        ("  FinAlly ", "bold bright_yellow"),
        ("Market Data", "bold bright_white"),
        ("  |  ", "bright_black"),
        (source_name, "bold bright_magenta"),
        ("  ", ""),
    )
    header_text.append_text(_health_markup(health))
    header_text.append_text(
        Text.assemble(
            ("  |  ", "bright_black"),
            (f"{elapsed:5.1f}s / {DURATION}s", "bright_cyan"),
            ("  |  ", "bright_black"),
            (f"{len(tickers)} tickers", "bright_white"),
            ("  |  ", "bright_black"),
            (f"{remaining:4.1f}s left", "bright_cyan"),
            ("  |  ", "bright_black"),
            ("Ctrl+C to exit", "bright_black italic"),
        )
    )
    layout["header"].update(Panel(header_text, border_style="bright_yellow"))

    layout["body"].update(
        Panel(
            build_table(tickers, prices, history),
            title="[bold bright_white]Live Prices[/]  [bright_black](Tick = flash vs last tick · Day %% = vs day open)[/]",
            border_style="bright_black",
        )
    )

    layout["footer"].update(build_event_log(events))
    return layout


def print_summary(prices: dict, tickers: list[str]) -> None:
    """Print final summary: seed price, final price, and daily change."""
    console = Console()
    console.print()
    console.print("[bold bright_yellow]  FinAlly[/] [bold]Session Summary[/]")
    console.print()

    table = Table(border_style="bright_black", header_style="bold bright_white", expand=False)
    table.add_column("Ticker", style="bold bright_white", width=8)
    table.add_column("Seed", justify="right", width=11)
    table.add_column("Final", justify="right", width=11)
    table.add_column("Day Open", justify="right", width=11)
    table.add_column("Day %", justify="right", width=10)

    for ticker in tickers:
        update = prices.get(ticker)
        if update is None:
            continue
        seed = SEED_PRICES.get(ticker)
        seed_str = f"${format_price(seed)}" if seed else "[bright_black]—[/]"

        day_pct = update.day_change_percent
        color = "green" if day_pct > 0 else ("red" if day_pct < 0 else "bright_black")
        table.add_row(
            ticker,
            seed_str,
            f"[{color}]${format_price(update.price)}[/]",
            f"${format_price(update.day_open)}",
            f"[{color}]{day_pct:+.2f}%[/]",
        )

    console.print(table)
    console.print()


def _log_shock(events: deque, ticker: str, update) -> None:
    """Append a notable-move event (a >1% per-tick jump = a shock)."""
    color, _ = _direction_style(update.direction)
    glyph = "▲" if update.direction == "up" else "▼"
    ts = time.strftime("%H:%M:%S")
    events.appendleft(
        f"[bright_black]{ts}[/]  [bold {color}]{glyph} {ticker}[/]  "
        f"[{color}]{update.change_percent:+.2f}%[/] tick  ${format_price(update.price)}"
    )


async def run() -> None:
    """Main demo loop."""
    cache = PriceCache()
    # Factory selects Simulator or Massive from MASSIVE_API_KEY, just like the app.
    source = create_market_data_source(cache)
    source_name = type(source).__name__.replace("DataSource", "")

    history: dict[str, deque] = defaultdict(lambda: deque(maxlen=40))
    events: deque = deque(maxlen=12)
    pending_actions = list(WATCHLIST_SCRIPT)

    await source.start(INITIAL_TICKERS)
    start_time = time.time()

    # Seed initial history from the first snapshot.
    _, prices = cache.snapshot()
    for ticker, update in prices.items():
        history[ticker].append(update.price)

    try:
        tickers = source.get_tickers()
        with Live(
            build_dashboard(
                tickers, prices, history, events, source.health(), source_name, start_time
            ),
            refresh_per_second=4,
            screen=True,
        ) as live:
            last_version = -1
            while time.time() - start_time < DURATION:
                await asyncio.sleep(0.25)
                elapsed = time.time() - start_time

                # Fire any scripted watchlist actions whose time has come.
                while pending_actions and elapsed >= pending_actions[0][0]:
                    _, action, raw = pending_actions.pop(0)
                    norm = normalize_ticker(raw)
                    ts = time.strftime("%H:%M:%S")
                    if action == "add":
                        await source.add_ticker(raw)
                        events.appendleft(
                            f"[bright_black]{ts}[/]  [bold bright_blue]+ watchlist[/]  "
                            f"added [bold]{norm}[/] [bright_black](typed '{raw.strip()}')[/]"
                        )
                    else:
                        await source.remove_ticker(raw)
                        events.appendleft(
                            f"[bright_black]{ts}[/]  [bold bright_magenta]- watchlist[/]  "
                            f"removed [bold]{norm}[/]"
                        )

                # Atomic (version, prices) read — the SSE change-detection path.
                version, prices = cache.snapshot()
                if version == last_version:
                    # No new data; still refresh so the health/clock tick along.
                    live.update(
                        build_dashboard(
                            source.get_tickers(), prices, history, events,
                            source.health(), source_name, start_time,
                        )
                    )
                    continue
                last_version = version

                for ticker, update in prices.items():
                    history[ticker].append(update.price)
                    # A >1% move between ticks is a simulator shock event.
                    if abs(update.change_percent) > 1.0:
                        _log_shock(events, ticker, update)

                live.update(
                    build_dashboard(
                        source.get_tickers(), prices, history, events,
                        source.health(), source_name, start_time,
                    )
                )

    except KeyboardInterrupt:
        pass
    finally:
        await source.stop()

    _, prices = cache.snapshot()
    print_summary(prices, source.get_tickers())


if __name__ == "__main__":
    asyncio.run(run())
