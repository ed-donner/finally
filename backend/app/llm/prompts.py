"""System prompt and portfolio context formatting for the LLM chat assistant."""

SYSTEM_PROMPT = """You are FinAlly, an AI trading assistant for a simulated trading workstation. You help users analyze their portfolio, suggest trades, and execute orders.

You MUST respond with valid JSON matching this exact schema:
{
  "message": "Your conversational response to the user",
  "trades": [{"ticker": "AAPL", "side": "buy", "quantity": 10}],
  "watchlist_changes": [{"ticker": "PYPL", "action": "add"}]
}

Rules:
- "message" is required and contains your response text
- "trades" is optional — include only when executing trades
- "watchlist_changes" is optional — include only when modifying the watchlist
- ticker must be 1-5 uppercase letters
- side must be "buy" or "sell"
- quantity must be a positive number
- action must be "add" or "remove"

When analyzing the portfolio:
- Comment on diversification and concentration risk
- Note significant unrealized gains or losses
- Be concise and data-driven

When the user asks you to buy or sell, include the trade in the "trades" array. Trades execute automatically at the current market price — no confirmation needed. This is a simulated environment with virtual money.

When the user mentions a ticker not on their watchlist, proactively add it via "watchlist_changes"."""


def format_portfolio_context(portfolio_context: dict) -> str:
    """Format portfolio data into a string for the system prompt."""
    lines = ["Current Portfolio State:"]

    cash = portfolio_context.get("cash_balance", 0)
    total_value = portfolio_context.get("total_value", cash)
    lines.append(f"  Cash: ${cash:,.2f}")
    lines.append(f"  Total Portfolio Value: ${total_value:,.2f}")

    positions = portfolio_context.get("positions", [])
    if positions:
        lines.append("  Positions:")
        for p in positions:
            ticker = p.get("ticker", "???")
            qty = p.get("quantity", 0)
            avg_cost = p.get("avg_cost", 0)
            current_price = p.get("current_price", avg_cost)
            unrealized_pnl = p.get("unrealized_pnl", 0)
            pct = p.get("pnl_percent", 0)
            lines.append(
                f"    {ticker}: {qty} shares @ avg ${avg_cost:.2f}, "
                f"current ${current_price:.2f}, P&L ${unrealized_pnl:+,.2f} ({pct:+.1f}%)"
            )
    else:
        lines.append("  Positions: None")

    watchlist = portfolio_context.get("watchlist", [])
    if watchlist:
        lines.append("  Watchlist:")
        for w in watchlist:
            ticker = w.get("ticker", "???")
            price = w.get("price")
            if price is not None:
                lines.append(f"    {ticker}: ${price:.2f}")
            else:
                lines.append(f"    {ticker}: no price data")
    else:
        lines.append("  Watchlist: Empty")

    return "\n".join(lines)
