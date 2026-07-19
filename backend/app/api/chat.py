"""Chat endpoint: LLM conversation with auto-executed trades & watchlist changes."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.db import repo
from app.llm import complete_chat
from app.runtime import get_market_source, get_price_cache
from app.services.portfolio import TradeError, build_portfolio, execute_trade

from .watchlist import build_watchlist

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


class ChatBody(BaseModel):
    message: str = Field(..., min_length=1)


async def _apply_trades(trades) -> list[dict]:
    """Execute each requested trade, returning per-action status records."""
    cache = get_price_cache()
    results: list[dict] = []
    for t in trades:
        ticker = t.ticker.upper()
        record = {
            "ticker": ticker,
            "side": t.side,
            "quantity": t.quantity,
            "status": "executed",
            "error": None,
        }
        price = cache.get_price(ticker)
        try:
            if price is None:
                raise TradeError(f"No price available for {ticker}.")
            execute_trade(ticker, t.side, t.quantity, price)
        except TradeError as exc:
            record["status"] = "failed"
            record["error"] = str(exc)
        results.append(record)
    return results


async def _apply_watchlist(changes) -> list[dict]:
    """Apply each watchlist change, returning per-action status records."""
    source = get_market_source()
    results: list[dict] = []
    for c in changes:
        ticker = c.ticker.upper()
        record = {"ticker": ticker, "action": c.action, "status": "applied", "error": None}
        try:
            if c.action == "add":
                if repo.add_watchlist(ticker):
                    await source.add_ticker(ticker)
                else:
                    record["status"] = "failed"
                    record["error"] = f"{ticker} already on watchlist."
            elif c.action == "remove":
                if repo.remove_watchlist(ticker):
                    await source.remove_ticker(ticker)
                else:
                    record["status"] = "failed"
                    record["error"] = f"{ticker} not on watchlist."
        except Exception as exc:  # pragma: no cover - source failures
            record["status"] = "failed"
            record["error"] = str(exc)
        results.append(record)
    return results


@router.post("/chat")
async def post_chat(body: ChatBody) -> dict:
    user_message = body.message.strip()

    portfolio = build_portfolio()
    watchlist = build_watchlist()["watchlist"]
    history = repo.get_chat_messages(limit=20)

    repo.insert_chat_message("user", user_message, None)

    response = complete_chat(user_message, portfolio, watchlist, history)

    trade_results = await _apply_trades(response.trades)
    watchlist_results = await _apply_watchlist(response.watchlist_changes)

    actions = {"trades": trade_results, "watchlist_changes": watchlist_results}
    repo.insert_chat_message("assistant", response.message, actions if (trade_results or watchlist_results) else None)

    return {
        "message": response.message,
        "trades": trade_results,
        "watchlist_changes": watchlist_results,
        "portfolio": build_portfolio(),
    }
