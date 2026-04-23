"""Deterministic mock LLM responses for E2E tests and key-less development.

Pattern-matches a small grammar of common requests. Anything that doesn't match
falls back to a generic conversational reply with no actions.
"""

from __future__ import annotations

import re

from .schema import LLMResponse, TradeRequest, WatchlistChange

# "buy 10 AAPL" / "sell 0.5 NVDA"
_TRADE_RE = re.compile(
    r"\b(buy|sell)\s+(\d+(?:\.\d+)?)\s+([A-Za-z]{1,5})\b", re.IGNORECASE
)

# "add PYPL to watchlist" / "remove TSLA from watchlist"
_WATCH_RE = re.compile(
    r"\b(add|remove)\s+([A-Za-z]{1,5})\b(?:.*?(?:to|from)\s+(?:the\s+)?watchlist)?",
    re.IGNORECASE,
)


def mock_response(user_message: str) -> LLMResponse:
    trades: list[TradeRequest] = []
    watchlist_changes: list[WatchlistChange] = []

    for verb, qty, ticker in _TRADE_RE.findall(user_message):
        trades.append(TradeRequest(
            ticker=ticker.upper(),
            side="buy" if verb.lower() == "buy" else "sell",
            quantity=float(qty),
            intent="execute",
        ))

    for verb, ticker in _WATCH_RE.findall(user_message):
        if "watchlist" in user_message.lower():
            watchlist_changes.append(WatchlistChange(
                ticker=ticker.upper(),
                action="add" if verb.lower() == "add" else "remove",
            ))

    if trades or watchlist_changes:
        actions: list[str] = []
        for t in trades:
            actions.append(f"{t.side} {t.quantity:g} {t.ticker}")
        for w in watchlist_changes:
            actions.append(f"{w.action} {w.ticker} {'to' if w.action == 'add' else 'from'} watchlist")
        message = "Proposed actions: " + ", ".join(actions) + "."
    else:
        message = (
            "[mock] Chat is running in deterministic mock mode. I can simulate trades like "
            "'buy 10 AAPL' or watchlist updates like 'add PYPL to watchlist'."
        )

    return LLMResponse(message=message, trades=trades, watchlist_changes=watchlist_changes)
