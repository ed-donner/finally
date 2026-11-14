"""Deterministic mock LLM for LLM_MOCK=true (E2E tests, offline development).

Rules (case-insensitive), per planning/TEAM_CONTRACTS.md §6:
  - "buy <N> <TICKER>"            -> buy trade,  reply starts with "Mock: buying"
  - "sell <N> <TICKER>"           -> sell trade
  - "add <TICKER>" / "watch <TICKER>" -> watchlist add
  - "remove <TICKER>"             -> watchlist remove
  - anything else                 -> MOCK_DEFAULT_MESSAGE, no actions
"""

from __future__ import annotations

import os
import re

from .schemas import ChatLLMResponse, TradeInstruction, WatchlistChange

MOCK_DEFAULT_MESSAGE = "Mock response: I'm FinAlly, your AI trading assistant."

# Ticker: starts with a letter; internal '.' or '-' allowed but not trailing ("AAPL." -> AAPL).
_TICKER = r"([A-Za-z][A-Za-z0-9]*(?:[.\-][A-Za-z0-9]+)*)"
_QTY = r"(\d+(?:\.\d+)?)"
_TRADE_RE = re.compile(rf"\b(buy|sell)\s+{_QTY}\s+{_TICKER}", re.IGNORECASE)
_WATCH_RE = re.compile(rf"\b(add|watch|remove)\s+{_TICKER}", re.IGNORECASE)


def is_mock_enabled() -> bool:
    return os.getenv("LLM_MOCK", "").strip().lower() in {"1", "true", "yes", "on"}


def _fmt_qty(q: float) -> str:
    return str(int(q)) if q == int(q) else str(q)


def mock_response(message: str) -> ChatLLMResponse:
    trades: list[TradeInstruction] = []
    for side, qty, ticker in _TRADE_RE.findall(message):
        trades.append(
            TradeInstruction(ticker=ticker.upper(), side=side.lower(), quantity=float(qty))
        )

    changes: list[WatchlistChange] = []
    for verb, ticker in _WATCH_RE.findall(message):
        action = "remove" if verb.lower() == "remove" else "add"
        changes.append(WatchlistChange(ticker=ticker.upper(), action=action))

    if not trades and not changes:
        return ChatLLMResponse(message=MOCK_DEFAULT_MESSAGE, trades=[], watchlist_changes=[])

    # Buys first so a buy reply always starts with "Mock: buying".
    trades.sort(key=lambda t: 0 if t.side == "buy" else 1)
    parts = [
        f"Mock: {'buying' if t.side == 'buy' else 'selling'} {_fmt_qty(t.quantity)} {t.ticker}"
        for t in trades
    ]
    parts += [
        f"Mock: {'adding' if c.action == 'add' else 'removing'} {c.ticker} "
        f"{'to' if c.action == 'add' else 'from'} your watchlist"
        for c in changes
    ]
    return ChatLLMResponse(message=". ".join(parts) + ".", trades=trades, watchlist_changes=changes)
