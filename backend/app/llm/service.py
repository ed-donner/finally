"""Core chat orchestrator: context -> LLM -> parse -> execute actions -> persist."""

import json
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite
import litellm

from app.market.cache import PriceCache
from app.market.interface import MarketDataSource
from app.portfolio.service import execute_trade, get_portfolio
from app.portfolio.snapshots import record_snapshot
from app.watchlist.service import add_ticker, get_watchlist, remove_ticker

from .mock import get_mock_response
from .models import ChatLLMResponse, ChatResponse, TradeResult, WatchlistResult
from .prompt import build_system_prompt

logger = logging.getLogger(__name__)


def parse_llm_response(content: str) -> ChatLLMResponse:
    """Parse LLM response JSON with fallback for malformed output."""
    try:
        return ChatLLMResponse.model_validate_json(content)
    except Exception:
        return ChatLLMResponse(message=content, trades=[], watchlist_changes=[])


async def save_chat_message(
    db: aiosqlite.Connection, role: str, content: str, actions: dict | None = None
) -> None:
    """Save a chat message to the database."""
    now = datetime.now(timezone.utc).isoformat()
    actions_json = json.dumps(actions) if actions else None
    await db.execute(
        "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) "
        "VALUES (?, 'default', ?, ?, ?, ?)",
        (str(uuid4()), role, content, actions_json, now),
    )
    await db.commit()


async def load_chat_history(db: aiosqlite.Connection, limit: int = 20) -> list[dict]:
    """Load recent chat messages for LLM context, in chronological order."""
    rows = await db.execute_fetchall(
        "SELECT role, content FROM chat_messages WHERE user_id = 'default' "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return [{"role": row[0], "content": row[1]} for row in reversed(rows)]


async def process_chat_message(
    db: aiosqlite.Connection,
    price_cache: PriceCache,
    market_source: MarketDataSource,
    user_message: str,
) -> ChatResponse:
    """Full chat flow: context -> LLM -> parse -> execute -> persist -> respond."""
    # 1. Build portfolio context
    portfolio = await get_portfolio(db, price_cache)
    watchlist_rows = await get_watchlist(db)
    watchlist_prices = []
    for row in watchlist_rows:
        ticker = row["ticker"]
        price = price_cache.get_price(ticker)
        if price is not None:
            watchlist_prices.append({"ticker": ticker, "price": price})

    # 2. Load conversation history
    history = await load_chat_history(db)

    # 3. Build messages array
    system_prompt = build_system_prompt(portfolio, watchlist_prices)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # 4. Call LLM or mock
    if os.environ.get("LLM_MOCK", "").lower() == "true":
        content = get_mock_response(user_message)
    else:
        response = await litellm.acompletion(
            model="openrouter/openai/gpt-oss-120b",
            messages=messages,
            extra_body={
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "chat_response",
                        "strict": True,
                        "schema": ChatLLMResponse.model_json_schema(),
                    },
                },
                "provider": {
                    "order": ["Cerebras"],
                    "allow_fallbacks": True,
                },
            },
        )
        content = response.choices[0].message.content

    # 5. Parse response
    parsed = parse_llm_response(content)

    # 6. Execute trades
    trade_results: list[TradeResult] = []
    for trade in parsed.trades:
        try:
            result = await execute_trade(
                db, price_cache, trade.ticker, trade.side, trade.quantity
            )
            trade_results.append(
                TradeResult(
                    status="executed",
                    ticker=result["ticker"],
                    side=result["side"],
                    quantity=result["quantity"],
                    price=result["price"],
                    total=result["total"],
                )
            )
        except (ValueError, Exception) as e:
            trade_results.append(
                TradeResult(
                    status="failed",
                    ticker=trade.ticker,
                    side=trade.side,
                    error=str(e),
                )
            )

    # 7. Execute watchlist changes
    watchlist_results: list[WatchlistResult] = []
    for change in parsed.watchlist_changes:
        try:
            if change.action == "add":
                await add_ticker(db, change.ticker)
                await market_source.add_ticker(change.ticker.upper())
            else:
                await remove_ticker(db, change.ticker)
                await market_source.remove_ticker(change.ticker.upper())
            watchlist_results.append(
                WatchlistResult(
                    status="applied", ticker=change.ticker, action=change.action
                )
            )
        except Exception as e:
            error_msg = e.detail if hasattr(e, "detail") else str(e)
            watchlist_results.append(
                WatchlistResult(
                    status="failed",
                    ticker=change.ticker,
                    action=change.action,
                    error=error_msg,
                )
            )

    # 8. Record portfolio snapshot if any trades executed
    if any(r.status == "executed" for r in trade_results):
        await record_snapshot(db, price_cache)

    # 9. Persist messages
    await save_chat_message(db, "user", user_message)
    actions = {
        "trades": [r.model_dump() for r in trade_results],
        "watchlist_changes": [r.model_dump() for r in watchlist_results],
    }
    await save_chat_message(db, "assistant", parsed.message, actions)

    # 10. Return response
    return ChatResponse(
        message=parsed.message,
        trades=trade_results,
        watchlist_changes=watchlist_results,
    )
