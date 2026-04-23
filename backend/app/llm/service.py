"""Chat orchestration: compose the prompt, call the LLM, execute actions, persist."""

from __future__ import annotations

import logging

from .. import db
from ..market import MarketDataSource, PriceCache
from ..portfolio import service as portfolio_service
from ..validation import normalize_ticker
from . import client
from .prompt import SYSTEM_PROMPT, build_portfolio_context
from .schema import (
    ChatActions,
    LLMResponse,
    TradeActionResult,
    TradeRequest,
    WatchlistActionResult,
    WatchlistChange,
)

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 20  # most-recent N messages included in the LLM prompt


def _build_messages(user_message: str, cache: PriceCache) -> list[dict[str, str]]:
    """Compose system + portfolio context + recent history + new user message."""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": build_portfolio_context(cache)},
    ]
    for msg in db.chat.list_recent(limit=HISTORY_LIMIT):
        messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": user_message})
    return messages


def _resolve_trade_action(
    cache: PriceCache,
    t: TradeRequest,
    allow_trade_execution: bool,
) -> TradeActionResult:
    if t.intent == "recommend":
        return TradeActionResult(
            ticker=t.ticker.upper(),
            side=t.side,
            quantity=t.quantity,
            intent=t.intent,
            status="recommended",
        )
    if not allow_trade_execution:
        return TradeActionResult(
            ticker=t.ticker.upper(),
            side=t.side,
            quantity=t.quantity,
            intent=t.intent,
            status="approval_required",
            error="Trade execution requires explicit approval for this message",
        )

    try:
        result = portfolio_service.execute_trade(cache, t.ticker, t.side, t.quantity)
        return TradeActionResult(
            ticker=result.ticker,
            side=result.side,
            quantity=result.quantity,
            intent=t.intent,
            status="executed",
            price=result.price,
            cash_balance_after=result.cash_balance_after,
        )
    except portfolio_service.TradeError as e:
        return TradeActionResult(
            ticker=t.ticker.upper(),
            side=t.side,
            quantity=t.quantity,
            intent=t.intent,
            status="failed",
            error=str(e),
        )


async def _apply_watchlist_change(
    source: MarketDataSource, w: WatchlistChange
) -> WatchlistActionResult:
    try:
        ticker = normalize_ticker(w.ticker)
    except ValueError as e:
        return WatchlistActionResult(
            ticker=w.ticker.upper().strip(),
            action=w.action,
            ok=False,
            error=str(e),
        )
    if w.action == "add":
        added = db.watchlist.add_ticker(ticker)
        if not added:
            return WatchlistActionResult(
                ticker=ticker, action="add", ok=False,
                error=f"{ticker} already in watchlist",
            )
        await source.add_ticker(ticker)
    else:
        removed = db.watchlist.remove_ticker(ticker)
        if not removed:
            return WatchlistActionResult(
                ticker=ticker, action="remove", ok=False,
                error=f"{ticker} not in watchlist",
            )
        await source.remove_ticker(ticker)
    return WatchlistActionResult(ticker=ticker, action=w.action, ok=True)


async def handle_user_message(
    user_message: str,
    cache: PriceCache,
    source: MarketDataSource,
    allow_trade_execution: bool = False,
) -> tuple[db.ChatMessage, db.ChatMessage, ChatActions]:
    """Persist the user message, call the LLM, run any actions, persist the assistant turn.

    Returns (user_msg_row, assistant_msg_row, actions). The assistant_msg.actions
    field holds the JSON-serialised ChatActions for the frontend to render badges.
    """
    user_row = db.chat.append_message("user", user_message)

    messages = _build_messages(user_message, cache)
    llm: LLMResponse = client.complete_chat(messages, user_message)

    actions = ChatActions()
    for t in llm.trades:
        actions.trades.append(
            _resolve_trade_action(cache, t, allow_trade_execution=allow_trade_execution)
        )
    for w in llm.watchlist_changes:
        actions.watchlist_changes.append(await _apply_watchlist_change(source, w))

    actions_payload = None if actions.is_empty() else actions.model_dump()
    assistant_content = llm.message
    if any(t.status == "approval_required" for t in actions.trades):
        assistant_content += (
            "\n\nTrade execution was not attempted because this message was not approved "
            "for execution."
        )
    elif any(t.status == "recommended" for t in actions.trades):
        assistant_content += "\n\nNo trades were executed."
    assistant_row = db.chat.append_message(
        "assistant", assistant_content, actions=actions_payload,
    )
    return user_row, assistant_row, actions
