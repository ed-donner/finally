"""Mock LLM responses for testing (LLM_MOCK=true)."""

import re

from .models import LlmResponse, TradeAction, WatchlistChange


def mock_chat(user_message: str, context: dict) -> LlmResponse:
    """Return deterministic responses based on simple keyword matching."""
    msg = user_message.lower().strip()

    # Buy request: "buy 10 AAPL" or "buy 5 shares of MSFT"
    buy_match = re.search(r"buy\s+(\d+)\s+(?:shares?\s+(?:of\s+)?)?(\w+)", msg)
    if buy_match:
        qty = float(buy_match.group(1))
        ticker = buy_match.group(2).upper()
        return LlmResponse(
            message=f"Executing purchase of {int(qty)} shares of {ticker}.",
            trades=[TradeAction(ticker=ticker, side="buy", quantity=qty)],
        )

    # Sell request: "sell 10 AAPL" or "sell 5 shares of MSFT"
    sell_match = re.search(r"sell\s+(\d+)\s+(?:shares?\s+(?:of\s+)?)?(\w+)", msg)
    if sell_match:
        qty = float(sell_match.group(1))
        ticker = sell_match.group(2).upper()
        return LlmResponse(
            message=f"Executing sale of {int(qty)} shares of {ticker}.",
            trades=[TradeAction(ticker=ticker, side="sell", quantity=qty)],
        )

    # Add to watchlist: "watch PYPL" or "add PYPL to watchlist"
    watch_match = re.search(r"(?:watch|add)\s+(\w+)", msg)
    if watch_match and "watchlist" in msg or msg.startswith("watch "):
        ticker = watch_match.group(1).upper()
        return LlmResponse(
            message=f"Adding {ticker} to your watchlist.",
            watchlist_changes=[WatchlistChange(ticker=ticker, action="add")],
        )

    # Portfolio analysis
    if any(kw in msg for kw in ["portfolio", "positions", "holdings", "analysis"]):
        cash = context.get("cash", 0)
        positions = context.get("positions", [])
        total_value = context.get("total_value", cash)
        if positions:
            tickers = ", ".join(p["ticker"] for p in positions)
            return LlmResponse(
                message=(
                    f"Your portfolio is worth ${total_value:,.2f} with "
                    f"${cash:,.2f} in cash. You hold: {tickers}."
                ),
            )
        return LlmResponse(
            message=(
                f"You have ${cash:,.2f} in cash and no open positions. "
                "Consider starting with a diversified set of holdings."
            ),
        )

    # Default greeting / fallback
    return LlmResponse(
        message=(
            "I'm FinAlly, your AI trading assistant. I can analyze your portfolio, "
            "execute trades, and manage your watchlist. How can I help?"
        ),
    )
