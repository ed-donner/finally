"""Mock LLM responses for testing and development without an API key."""

import re

from app.llm.models import LLMResponse, TradeAction, WatchlistChange


_SKIP_WORDS = {
    "BUY", "SELL", "ADD", "REMOVE", "THE", "AND", "FOR", "NOT", "ALL", "CAN",
    "SOME", "MY", "YOUR", "FROM", "TO", "OF", "IN", "ON", "AT", "IT", "IS",
    "A", "I", "ME", "DO", "HOW", "WHAT", "SHOW", "GET", "SET", "PUT",
    "SHARES", "STOCK", "PRICE", "VALUE", "MUCH", "MANY", "WITH",
}


def _extract_ticker(message: str) -> str | None:
    """Extract an uppercase ticker (1-5 letters) from the message."""
    candidates = re.findall(r"\b([A-Z]{1,5})\b", message.upper())
    for candidate in candidates:
        if candidate not in _SKIP_WORDS:
            return candidate
    return None


async def mock_chat_with_llm(
    user_message: str,
    portfolio_context: dict,
    conversation_history: list[dict],
) -> LLMResponse:
    """Return deterministic mock responses based on message content."""
    msg_lower = user_message.lower()

    if "buy" in msg_lower:
        ticker = _extract_ticker(user_message) or "AAPL"
        return LLMResponse(
            message=f"Mock: Buying 10 shares of {ticker}.",
            trades=[TradeAction(ticker=ticker, side="buy", quantity=10)],
        )

    if "sell" in msg_lower:
        ticker = _extract_ticker(user_message) or "AAPL"
        return LLMResponse(
            message=f"Mock: Selling 5 shares of {ticker}.",
            trades=[TradeAction(ticker=ticker, side="sell", quantity=5)],
        )

    if "add" in msg_lower:
        ticker = _extract_ticker(user_message) or "PYPL"
        return LLMResponse(
            message=f"Mock: Adding {ticker} to your watchlist.",
            watchlist_changes=[WatchlistChange(ticker=ticker, action="add")],
        )

    if "remove" in msg_lower:
        ticker = _extract_ticker(user_message) or "AAPL"
        return LLMResponse(
            message=f"Mock: Removing {ticker} from your watchlist.",
            watchlist_changes=[WatchlistChange(ticker=ticker, action="remove")],
        )

    return LLMResponse(
        message="Mock: I'm your AI trading assistant. How can I help you today?"
    )
