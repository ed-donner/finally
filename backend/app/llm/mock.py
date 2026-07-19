"""Deterministic mock LLM (LLM_MOCK=true).

Implements the exact rules the Integration Tester asserts on (TEAM_CONTRACT §4):
- "buy N TICKER"   -> a buy trade
- "sell N TICKER"  -> a sell trade
- "add TICKER"     -> watchlist add
- "remove TICKER"  -> watchlist remove
- anything else    -> a canned analytical reply, no actions

No network. Pure string parsing.
"""

from __future__ import annotations

import re

from .schema import ChatResponse, LLMTrade, WatchlistChange

_TRADE_RE = re.compile(r"\b(buy|sell)\s+(\d+(?:\.\d+)?)\s+([A-Za-z]{1,5})\b", re.IGNORECASE)
_WATCH_RE = re.compile(r"\b(add|remove)\s+([A-Za-z]{1,5})\b", re.IGNORECASE)


def mock_response(user_message: str, portfolio: dict) -> ChatResponse:
    trades: list[LLMTrade] = []
    watchlist_changes: list[WatchlistChange] = []

    for side, qty, ticker in _TRADE_RE.findall(user_message):
        trades.append(
            LLMTrade(ticker=ticker.upper(), side=side.lower(), quantity=float(qty))
        )

    for action, ticker in _WATCH_RE.findall(user_message):
        watchlist_changes.append(
            WatchlistChange(ticker=ticker.upper(), action=action.lower())
        )

    if trades:
        summary = ", ".join(f"{t.side} {t.quantity:g} {t.ticker}" for t in trades)
        message = f"Executing: {summary}. I'll update your portfolio."
    elif watchlist_changes:
        summary = ", ".join(f"{c.action} {c.ticker}" for c in watchlist_changes)
        message = f"Updating your watchlist: {summary}."
    else:
        total = portfolio.get("total_value", 0.0)
        pl = portfolio.get("total_unrealized_pl", 0.0)
        n = len(portfolio.get("positions", []))
        message = (
            f"[mock] Your portfolio is worth ${total:,.2f} across {n} position(s) "
            f"with ${pl:,.2f} unrealized P&L. Ask me to buy or sell (e.g. 'buy 5 AAPL') "
            f"or to add/remove a ticker from your watchlist."
        )

    return ChatResponse(message=message, trades=trades, watchlist_changes=watchlist_changes)
