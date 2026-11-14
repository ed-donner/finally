"""Chat orchestration: context -> LLM (or mock) -> auto-execute actions -> persist."""

from __future__ import annotations

import logging

from app import db
from app.market import MarketDataSource, PriceCache
from app.services import portfolio as portfolio_service
from app.services import watchlist as watchlist_service

from . import client
from .mock import is_mock_enabled, mock_response
from .prompts import HISTORY_LIMIT, build_messages, build_portfolio_context
from .schemas import ChatLLMResponse, TradeInstruction, WatchlistChange, parse_llm_response

logger = logging.getLogger(__name__)

NOT_CONFIGURED_MESSAGE = (
    "The AI assistant isn't configured yet: set OPENROUTER_API_KEY in the .env file "
    "(or LLM_MOCK=true for mock responses). You can still trade and manage your "
    "watchlist manually."
)
LLM_ERROR_MESSAGE = (
    "Sorry, I'm having trouble reaching the AI service right now. Please try again in a moment."
)


async def _get_llm_response(message: str, price_cache: PriceCache) -> ChatLLMResponse:
    """Never raises: errors become a friendly message with no actions."""
    if is_mock_enabled():
        return mock_response(message)
    if not client.is_configured():
        return ChatLLMResponse(message=NOT_CONFIGURED_MESSAGE, trades=[], watchlist_changes=[])
    try:
        history = db.get_chat_messages(limit=HISTORY_LIMIT)
        context = build_portfolio_context(price_cache)
        raw = await client.call_llm(build_messages(message, context, history))
    except Exception:
        logger.exception("LLM call failed")
        return ChatLLMResponse(message=LLM_ERROR_MESSAGE, trades=[], watchlist_changes=[])
    return parse_llm_response(raw)


async def _ensure_price(ticker: str, price_cache: PriceCache, source: MarketDataSource) -> None:
    """Start tracking an unknown ticker so a buy can fill (the simulator seeds a price on
    add). Untracked leftovers are dropped again by sync_tracked_tickers afterwards."""
    if price_cache.get_price(ticker) is not None:
        return
    try:
        portfolio_service.validate_ticker(ticker)
        await source.add_ticker(ticker)
    except Exception:
        logger.warning("Could not start tracking %s before trade", ticker)


async def _execute_trade(
    trade: TradeInstruction, price_cache: PriceCache, source: MarketDataSource
) -> dict:
    if trade.side == "buy":
        await _ensure_price(trade.ticker, price_cache, source)
    result = {
        "ticker": trade.ticker,
        "side": trade.side,
        "quantity": trade.quantity,
        "status": "failed",
        "price": None,
        "error": None,
    }
    try:
        executed = portfolio_service.execute_trade(
            trade.ticker, trade.side, trade.quantity, price_cache
        )
    except portfolio_service.TradeError as exc:
        result["error"] = str(exc)
        return result
    except Exception:
        logger.exception("Unexpected error executing trade %s", trade)
        result["error"] = "Trade could not be executed due to an internal error"
        return result
    result.update(
        status="executed",
        ticker=executed.get("ticker", trade.ticker),
        quantity=executed.get("quantity", trade.quantity),
        price=executed.get("price"),
    )
    return result


async def _apply_watchlist_change(change: WatchlistChange, source: MarketDataSource) -> dict:
    result = {"ticker": change.ticker, "action": change.action, "status": "failed", "error": None}
    try:
        if change.action == "add":
            await watchlist_service.add_ticker(change.ticker, source)
        elif not await watchlist_service.remove_ticker(change.ticker, source):
            result["error"] = f"{change.ticker} is not in the watchlist"
            return result
    except Exception as exc:  # ValueError (invalid ticker), duplicates, etc.
        result["error"] = str(exc) or f"Could not {change.action} {change.ticker}"
        return result
    result["status"] = "executed"
    return result


def _failure_note(trades: list[dict], changes: list[dict]) -> str:
    lines = []
    for t in trades:
        if t["status"] == "failed":
            lines.append(f"- {t['side']} {t['quantity']:g} {t['ticker']} failed: {t['error']}")
    for c in changes:
        if c["status"] == "failed":
            lines.append(f"- {c['action']} {c['ticker']} failed: {c['error']}")
    return ("\n\nSome actions could not be completed:\n" + "\n".join(lines)) if lines else ""


async def handle_chat(message: str, price_cache: PriceCache, source: MarketDataSource) -> dict:
    """Process one user chat message. Returns the POST /api/chat response shape."""
    message = message.strip()
    llm = await _get_llm_response(message, price_cache)

    trade_results = [await _execute_trade(t, price_cache, source) for t in llm.trades]
    change_results = [await _apply_watchlist_change(c, source) for c in llm.watchlist_changes]

    if trade_results or change_results:
        try:
            await watchlist_service.sync_tracked_tickers(source)
        except Exception:
            logger.exception("sync_tracked_tickers failed after chat actions")

    reply = llm.message + _failure_note(trade_results, change_results)
    response = {"message": reply, "trades": trade_results, "watchlist_changes": change_results}

    try:
        db.add_chat_message("user", message)
        db.add_chat_message(
            "assistant",
            reply,
            {"trades": trade_results, "watchlist_changes": change_results},
        )
    except Exception:
        logger.exception("Failed to persist chat messages")
    return response


def get_chat_history(limit: int = 100) -> list[dict]:
    rows = db.get_chat_messages(limit=limit)
    return [
        {
            "id": r.get("id"),
            "role": r.get("role"),
            "content": r.get("content"),
            "actions": r.get("actions"),
            "created_at": r.get("created_at"),
        }
        for r in rows
    ]
