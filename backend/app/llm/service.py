"""LLM chat service — calls OpenRouter/Cerebras and executes actions."""

import logging
import os

from litellm import completion

from app.db import (
    add_to_watchlist,
    delete_position,
    get_cash_balance,
    get_chat_history,
    get_position,
    get_positions,
    get_watchlist,
    insert_snapshot,
    insert_trade,
    remove_from_watchlist,
    update_cash_balance,
    upsert_position,
)
from app.market import PriceCache

from .mock import mock_chat
from .models import LlmResponse

logger = logging.getLogger(__name__)

MODEL = "openrouter/openai/gpt-oss-120b"
EXTRA_BODY = {"provider": {"order": ["cerebras"]}}

SYSTEM_PROMPT = """You are FinAlly, an AI trading assistant for a simulated trading workstation.

You help users manage a virtual stock portfolio. You can:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with clear reasoning
- Execute trades when asked (buy or sell shares)
- Add or remove tickers from the watchlist

Rules:
- This is a simulation with virtual money — no real trades occur
- All orders are market orders filled at the current price
- Be concise and data-driven
- When the user asks you to trade, include the trade in your response
- When suggesting trades, explain your reasoning briefly
- Always respond with valid structured JSON matching the required schema"""


async def _build_context(price_cache: PriceCache) -> dict:
    """Build portfolio context to include in LLM prompt."""
    cash = await get_cash_balance()
    positions = await get_positions()
    watchlist = await get_watchlist()

    enriched_positions = []
    total_market_value = 0.0
    for pos in positions:
        current_price = price_cache.get_price(pos["ticker"]) or pos["avg_cost"]
        market_value = current_price * pos["quantity"]
        cost_basis = pos["avg_cost"] * pos["quantity"]
        unrealized_pnl = market_value - cost_basis
        enriched_positions.append({
            "ticker": pos["ticker"],
            "quantity": pos["quantity"],
            "avg_cost": round(pos["avg_cost"], 2),
            "current_price": round(current_price, 2),
            "market_value": round(market_value, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
        })
        total_market_value += market_value

    watchlist_with_prices = []
    for entry in watchlist:
        ticker = entry["ticker"]
        price = price_cache.get_price(ticker)
        watchlist_with_prices.append({
            "ticker": ticker,
            "price": round(price, 2) if price else None,
        })

    total_value = cash + total_market_value

    return {
        "cash": round(cash, 2),
        "positions": enriched_positions,
        "watchlist": watchlist_with_prices,
        "total_value": round(total_value, 2),
        "total_market_value": round(total_market_value, 2),
    }


def _build_messages(context: dict, history: list[dict], user_message: str) -> list[dict]:
    """Construct the messages list for the LLM call."""
    context_text = (
        f"Portfolio: Cash ${context['cash']:,.2f}, "
        f"Total value ${context['total_value']:,.2f}\n"
    )
    if context["positions"]:
        context_text += "Positions:\n"
        for p in context["positions"]:
            context_text += (
                f"  {p['ticker']}: {p['quantity']} shares @ avg ${p['avg_cost']:.2f}, "
                f"now ${p['current_price']:.2f}, P&L ${p['unrealized_pnl']:+.2f}\n"
            )
    else:
        context_text += "No open positions.\n"

    context_text += "Watchlist: " + ", ".join(
        f"{w['ticker']} (${w['price']:.2f})" if w["price"] else w["ticker"]
        for w in context["watchlist"]
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n\nCurrent portfolio state:\n" + context_text},
    ]

    # Add recent conversation history (last 20 messages)
    for msg in history[-20:]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": user_message})
    return messages


async def _execute_actions(
    llm_response: LlmResponse, price_cache: PriceCache
) -> dict:
    """Execute trades and watchlist changes from LLM response. Returns results."""
    trade_results = []
    watchlist_results = []

    for trade in llm_response.trades:
        ticker = trade.ticker.upper()
        side = trade.side.lower()
        quantity = trade.quantity

        if side not in ("buy", "sell") or quantity <= 0:
            trade_results.append({"ticker": ticker, "side": side, "error": "Invalid trade parameters"})
            continue

        current_price = price_cache.get_price(ticker)
        if current_price is None:
            trade_results.append({"ticker": ticker, "side": side, "error": f"No price available for {ticker}"})
            continue

        cash = await get_cash_balance()

        if side == "buy":
            cost = current_price * quantity
            if cost > cash:
                trade_results.append({
                    "ticker": ticker, "side": "buy",
                    "error": f"Insufficient cash. Need ${cost:.2f}, have ${cash:.2f}",
                })
                continue

            await update_cash_balance(cash - cost)
            existing = await get_position(ticker)
            if existing:
                total_qty = existing["quantity"] + quantity
                total_cost = (existing["avg_cost"] * existing["quantity"]) + cost
                new_avg = total_cost / total_qty
                await upsert_position(ticker, total_qty, new_avg)
            else:
                await upsert_position(ticker, quantity, current_price)

        else:  # sell
            existing = await get_position(ticker)
            if not existing or existing["quantity"] < quantity:
                held = existing["quantity"] if existing else 0
                trade_results.append({
                    "ticker": ticker, "side": "sell",
                    "error": f"Insufficient shares. Have {held}, trying to sell {quantity}",
                })
                continue

            proceeds = current_price * quantity
            await update_cash_balance(cash + proceeds)
            remaining = existing["quantity"] - quantity
            if remaining > 0:
                await upsert_position(ticker, remaining, existing["avg_cost"])
            else:
                await delete_position(ticker)

        await insert_trade(ticker, side, quantity, current_price)
        trade_results.append({
            "ticker": ticker, "side": side, "quantity": quantity,
            "price": current_price, "status": "executed",
        })

    # Record portfolio snapshot after trades
    if trade_results:
        new_cash = await get_cash_balance()
        positions = await get_positions()
        total_value = new_cash
        for pos in positions:
            price = price_cache.get_price(pos["ticker"]) or pos["avg_cost"]
            total_value += price * pos["quantity"]
        await insert_snapshot(round(total_value, 2))

    for change in llm_response.watchlist_changes:
        ticker = change.ticker.upper()
        action = change.action.lower()
        if action == "add":
            try:
                await add_to_watchlist(ticker)
                watchlist_results.append({"ticker": ticker, "action": "add", "status": "done"})
            except Exception as exc:
                watchlist_results.append({"ticker": ticker, "action": "add", "error": str(exc)})
        elif action == "remove":
            removed = await remove_from_watchlist(ticker)
            if removed:
                watchlist_results.append({"ticker": ticker, "action": "remove", "status": "done"})
            else:
                watchlist_results.append({"ticker": ticker, "action": "remove", "error": "Not in watchlist"})

    return {"trades": trade_results, "watchlist_changes": watchlist_results}


async def chat_with_llm(user_message: str, price_cache: PriceCache) -> dict:
    """Process a chat message: call LLM (or mock), execute actions, return response."""
    context = await _build_context(price_cache)

    # Mock mode
    if os.environ.get("LLM_MOCK", "").lower() == "true":
        llm_response = mock_chat(user_message, context)
        action_results = await _execute_actions(llm_response, price_cache)
        return {
            "message": llm_response.message,
            "trades": action_results["trades"],
            "watchlist_changes": action_results["watchlist_changes"],
        }

    # Real LLM call
    history = await get_chat_history(limit=20)
    messages = _build_messages(context, history, user_message)

    try:
        response = completion(
            model=MODEL,
            messages=messages,
            response_format=LlmResponse,
            reasoning_effort="low",
            extra_body=EXTRA_BODY,
        )
        content = response.choices[0].message.content
        llm_response = LlmResponse.model_validate_json(content)
    except Exception:
        logger.exception("LLM call failed")
        return {
            "message": "Sorry, I encountered an error processing your request. Please try again.",
            "trades": [],
            "watchlist_changes": [],
        }

    action_results = await _execute_actions(llm_response, price_cache)
    return {
        "message": llm_response.message,
        "trades": action_results["trades"],
        "watchlist_changes": action_results["watchlist_changes"],
    }
