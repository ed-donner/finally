"""System prompt + portfolio-context formatting for the LLM."""

from __future__ import annotations

from .. import db
from ..market import PriceCache
from ..portfolio import service

SYSTEM_PROMPT = """You are FinAlly, an AI trading assistant inside a simulated trading workstation.

You can:
  - Analyze the user's portfolio composition, P&L, and risk concentration
  - Suggest trades with concrete reasoning grounded in the live data shown to you
  - Return trade ideas in the `trades` array
  - Manage the watchlist by including entries in `watchlist_changes`

Style:
  - Be concise and data-driven
  - When proposing or executing a trade, briefly explain why
  - Return BOTH `trades` and `watchlist_changes` as arrays — empty arrays if no action

Constraints:
  - This is simulated money; do not refuse trades on risk grounds — the user has chosen this exposure
  - Quantities may be fractional
  - All tickers must be uppercase US-listed equities
  - Set trade `intent` to `recommend` for analysis, suggestions, or hypothetical ideas
  - Set trade `intent` to `execute` only when the user clearly asked to place the order
  - Never claim a trade is already filled before the server confirms it
"""


def build_portfolio_context(cache: PriceCache) -> str:
    """Render the user's current state as a compact text block for the LLM."""
    view = service.get_portfolio_view(cache)
    watchlist_tickers = db.watchlist.list_tickers()

    lines = [
        "## Portfolio snapshot",
        f"Cash: ${view.cash_balance:,.2f}",
        f"Total value: ${view.total_value:,.2f}",
        f"Unrealized P&L: ${view.unrealized_pnl:,.2f} ({view.unrealized_pnl_percent:+.2f}%)",
        "",
    ]

    if view.positions:
        lines.append("## Positions")
        lines.append("ticker | qty | avg_cost | price | mkt_value | pnl | pnl_%")
        for p in view.positions:
            price_str = f"${p.current_price:,.2f}" if p.current_price is not None else "n/a"
            lines.append(
                f"{p.ticker} | {p.quantity:g} | ${p.avg_cost:,.2f} | {price_str} | "
                f"${p.market_value:,.2f} | ${p.unrealized_pnl:+,.2f} | {p.unrealized_pnl_percent:+.2f}%"
            )
        lines.append("")
    else:
        lines.append("## Positions: none\n")

    lines.append("## Watchlist (with live prices)")
    for ticker in watchlist_tickers:
        price = cache.get_price(ticker)
        lines.append(f"{ticker}: ${price:,.2f}" if price is not None else f"{ticker}: n/a")

    return "\n".join(lines)
