"""Chat API endpoint — LLM integration with auto-execution."""

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.portfolio import _build_portfolio_summary
from app.db import (
    get_recent_chat_messages,
    insert_chat_message,
    insert_portfolio_snapshot,
    insert_trade,
    update_cash_balance,
    get_user_profile,
    get_position_by_ticker,
    list_watchlist,
    add_watchlist_ticker,
    remove_watchlist_ticker,
    upsert_position,
)
from app.llm import chat_with_llm, LLMResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)


@router.get("/chat")
async def get_chat_history():
    """Return recent chat messages."""
    messages = await get_recent_chat_messages(50)
    return messages


@router.post("/chat")
async def chat(body: ChatRequest, request: Request):
    """Send a message to the AI assistant."""
    cache = request.app.state.price_cache
    source = request.app.state.market_source

    # Store user message
    await insert_chat_message("user", body.message)

    # Build context
    portfolio = await _build_portfolio_summary(cache)
    watchlist_items = await list_watchlist()
    watchlist_tickers = [w["ticker"] for w in watchlist_items]
    history = await get_recent_chat_messages(20)

    # Build price context for watchlist
    prices = {}
    for ticker in watchlist_tickers:
        pu = cache.get(ticker)
        if pu:
            prices[ticker] = pu.to_dict()

    context = {
        "portfolio": portfolio,
        "watchlist": watchlist_tickers,
        "prices": prices,
    }

    # Call LLM
    try:
        llm_response = await chat_with_llm(body.message, context, history)
    except Exception:
        logger.exception("LLM call failed")
        llm_response = LLMResponse(
            message="I'm having trouble connecting right now, please try again in a moment."
        )

    # Auto-execute trades
    executed_trades = []
    trade_errors = []
    for trade_action in llm_response.trades:
        try:
            ticker = trade_action.ticker.upper()
            side = trade_action.side
            quantity = float(trade_action.quantity)

            price_update = cache.get(ticker)
            if not price_update:
                trade_errors.append(f"No price available for {ticker}")
                continue

            current_price = price_update.price
            profile = await get_user_profile()
            cash = profile["cash_balance"]

            if side == "buy":
                cost = current_price * quantity
                if cost > cash:
                    trade_errors.append(
                        f"Insufficient cash to buy {quantity} {ticker} (need ${cost:.2f}, have ${cash:.2f})"
                    )
                    continue
                new_cash = cash - cost
                existing = await get_position_by_ticker(ticker)
                if existing:
                    total_qty = existing["quantity"] + quantity
                    new_avg = (existing["avg_cost"] * existing["quantity"] + cost) / total_qty
                else:
                    total_qty = quantity
                    new_avg = current_price
                await update_cash_balance(new_cash)
                await upsert_position(ticker, total_qty, new_avg)

            elif side == "sell":
                existing = await get_position_by_ticker(ticker)
                if not existing or existing["quantity"] < quantity:
                    owned = existing["quantity"] if existing else 0
                    trade_errors.append(
                        f"Insufficient shares to sell {quantity} {ticker} (own {owned})"
                    )
                    continue
                proceeds = current_price * quantity
                new_cash = cash + proceeds
                remaining = existing["quantity"] - quantity
                await update_cash_balance(new_cash)
                await upsert_position(ticker, remaining, existing["avg_cost"])

            trade_record = await insert_trade(ticker, side, quantity, current_price)
            executed_trades.append(trade_record)

        except Exception:
            logger.exception("Failed to execute LLM trade")
            trade_errors.append(f"Failed to execute {trade_action.side} {trade_action.quantity} {trade_action.ticker}")

    # Auto-execute watchlist changes
    watchlist_results = []
    for wl_change in llm_response.watchlist_changes:
        try:
            ticker = wl_change.ticker.upper()
            action = wl_change.action
            if action == "add":
                await add_watchlist_ticker(ticker)
                await source.add_ticker(ticker)
                watchlist_results.append({"ticker": ticker, "action": "added"})
            elif action == "remove":
                await remove_watchlist_ticker(ticker)
                await source.remove_ticker(ticker)
                watchlist_results.append({"ticker": ticker, "action": "removed"})
        except Exception as e:
            logger.warning("Watchlist change failed: %s", e)
            watchlist_results.append({"ticker": wl_change.ticker, "error": str(e)})

    # Snapshot portfolio after any trades
    if executed_trades:
        updated_portfolio = await _build_portfolio_summary(cache)
        await insert_portfolio_snapshot(updated_portfolio["total_value"])

    # Build actions summary
    actions = None
    if executed_trades or watchlist_results or trade_errors:
        actions = {
            "trades": executed_trades,
            "watchlist_changes": watchlist_results,
            "errors": trade_errors,
        }

    # Append error info to the message if trades failed
    assistant_message = llm_response.message
    if trade_errors:
        assistant_message += "\n\n(Some actions could not be completed: " + "; ".join(trade_errors) + ")"

    # Store assistant message
    stored = await insert_chat_message("assistant", assistant_message, actions)

    return {
        "message": {
            "id": stored["id"],
            "role": "assistant",
            "content": assistant_message,
            "actions": actions,
            "created_at": stored["created_at"],
        }
    }
