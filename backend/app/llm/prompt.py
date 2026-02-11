"""System prompt builder for LLM chat context."""


def build_system_prompt(portfolio: dict, watchlist_prices: list[dict]) -> str:
    """Build a system prompt with live portfolio state and watchlist prices.

    Args:
        portfolio: Dict from get_portfolio() with cash_balance, positions, total_value.
        watchlist_prices: List of dicts with ticker and price from the watchlist.
    """
    positions_text = ""
    for p in portfolio["positions"]:
        positions_text += (
            f"  {p['ticker']}: {p['quantity']} shares, "
            f"avg cost ${p['avg_cost']:.2f}, "
            f"current ${p['current_price']:.2f}, "
            f"P&L ${p['unrealized_pnl']:.2f} ({p['unrealized_pnl_percent']:+.1f}%)\n"
        )

    watchlist_text = ""
    for w in watchlist_prices:
        watchlist_text += f"  {w['ticker']}: ${w['price']:.2f}\n"

    return f"""You are FinAlly, an AI trading assistant for a simulated portfolio.
You analyze positions, suggest trades, execute trades, and manage the watchlist.
Be concise and data-driven. Always respond with valid JSON.

Current portfolio:
  Cash: ${portfolio['cash_balance']:.2f}
  Total value: ${portfolio['total_value']:.2f}
  Positions:
{positions_text or '  (none)'}
Watchlist prices:
{watchlist_text or '  (none)'}
You MUST respond with JSON matching this exact schema:
{{
  "message": "your response text",
  "trades": [{{"ticker": "AAPL", "side": "buy", "quantity": 10}}],
  "watchlist_changes": [{{"ticker": "PYPL", "action": "add"}}]
}}
trades and watchlist_changes are optional arrays (use empty arrays if no actions needed)."""
