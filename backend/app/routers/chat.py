"""Chat API endpoint with LLM integration."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db.connection import get_db
from ..market.cache import PriceCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

_price_cache: PriceCache | None = None

MODEL = "openrouter/openai/gpt-oss-120b"
EXTRA_BODY = {"provider": {"order": ["cerebras"]}}


def set_price_cache(cache: PriceCache) -> None:
    global _price_cache
    _price_cache = cache


class ChatRequest(BaseModel):
    message: str


class TradeAction(BaseModel):
    ticker: str
    side: str
    quantity: float


class WatchlistChange(BaseModel):
    ticker: str
    action: str  # "add" or "remove"


class LLMResponse(BaseModel):
    message: str
    trades: list[TradeAction] = []
    watchlist_changes: list[WatchlistChange] = []


def _get_portfolio_context(db: Any) -> str:
    """Build a text description of the user's current portfolio for the LLM."""
    profile = db.execute(
        "SELECT cash_balance FROM users_profile WHERE id = 'default'"
    ).fetchone()
    cash = profile["cash_balance"] if profile else 10000.0

    positions_rows = db.execute(
        "SELECT ticker, quantity, avg_cost FROM positions WHERE user_id = 'default' AND quantity > 0"
    ).fetchall()

    watchlist_rows = db.execute(
        "SELECT ticker FROM watchlist WHERE user_id = 'default' ORDER BY added_at"
    ).fetchall()

    positions_text = []
    total_pos_value = 0.0
    for row in positions_rows:
        ticker = row["ticker"]
        qty = row["quantity"]
        avg = row["avg_cost"]
        price = (_price_cache.get_price(ticker) if _price_cache else None) or avg
        mkt_val = qty * price
        pnl = mkt_val - qty * avg
        pnl_pct = (pnl / (qty * avg) * 100) if avg else 0
        total_pos_value += mkt_val
        positions_text.append(
            f"  {ticker}: {qty} shares @ avg ${avg:.2f}, current ${price:.2f}, "
            f"value ${mkt_val:.2f}, P&L ${pnl:+.2f} ({pnl_pct:+.1f}%)"
        )

    total_value = cash + total_pos_value
    watchlist_tickers = [r["ticker"] for r in watchlist_rows]

    watchlist_prices = []
    for t in watchlist_tickers:
        price = _price_cache.get_price(t) if _price_cache else None
        price_str = f"${price:.2f}" if price else "N/A"
        watchlist_prices.append(f"{t}: {price_str}")

    ctx_parts = [
        f"Cash: ${cash:.2f}",
        f"Total Portfolio Value: ${total_value:.2f}",
        f"Positions Value: ${total_pos_value:.2f}",
    ]
    if positions_text:
        ctx_parts.append("Positions:\n" + "\n".join(positions_text))
    else:
        ctx_parts.append("Positions: None (no open positions)")

    ctx_parts.append("Watchlist: " + ", ".join(watchlist_prices))

    return "\n".join(ctx_parts)


def _execute_llm_trades(db: Any, trades: list[TradeAction], now: str) -> list[dict[str, Any]]:
    """Execute trades from LLM response. Returns list of results."""
    from ..routers.portfolio import _get_portfolio_data

    results = []
    for trade in trades:
        ticker = trade.ticker.upper()
        side = trade.side.lower()
        quantity = trade.quantity

        try:
            price = (_price_cache.get_price(ticker) if _price_cache else None)
            if price is None:
                results.append({"ticker": ticker, "side": side, "error": "No price available"})
                continue

            if side == "buy":
                cost = quantity * price
                profile = db.execute("SELECT cash_balance FROM users_profile WHERE id = 'default'").fetchone()
                if profile["cash_balance"] < cost:
                    results.append({"ticker": ticker, "side": side, "error": f"Insufficient cash (need ${cost:.2f})"})
                    continue
                db.execute("UPDATE users_profile SET cash_balance = cash_balance - ? WHERE id = 'default'", (cost,))
                existing = db.execute("SELECT quantity, avg_cost FROM positions WHERE user_id = 'default' AND ticker = ?", (ticker,)).fetchone()
                if existing:
                    old_qty = existing["quantity"]
                    old_avg = existing["avg_cost"]
                    new_qty = old_qty + quantity
                    new_avg = (old_qty * old_avg + quantity * price) / new_qty
                    db.execute("UPDATE positions SET quantity = ?, avg_cost = ?, updated_at = ? WHERE user_id = 'default' AND ticker = ?", (new_qty, new_avg, now, ticker))
                else:
                    db.execute("INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) VALUES (?, 'default', ?, ?, ?, ?)", (str(uuid.uuid4()), ticker, quantity, price, now))
            else:  # sell
                existing = db.execute("SELECT quantity FROM positions WHERE user_id = 'default' AND ticker = ?", (ticker,)).fetchone()
                if not existing or existing["quantity"] < quantity:
                    owned = existing["quantity"] if existing else 0
                    results.append({"ticker": ticker, "side": side, "error": f"Insufficient shares (own {owned})"})
                    continue
                proceeds = quantity * price
                new_qty = existing["quantity"] - quantity
                db.execute("UPDATE users_profile SET cash_balance = cash_balance + ? WHERE id = 'default'", (proceeds,))
                if new_qty > 0.0001:
                    db.execute("UPDATE positions SET quantity = ?, updated_at = ? WHERE user_id = 'default' AND ticker = ?", (new_qty, now, ticker))
                else:
                    db.execute("DELETE FROM positions WHERE user_id = 'default' AND ticker = ?", (ticker,))

            db.execute(
                "INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at) VALUES (?, 'default', ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), ticker, side, quantity, price, now),
            )
            results.append({"ticker": ticker, "side": side, "quantity": quantity, "price": price, "success": True})
        except Exception as e:
            logger.error("Trade execution error: %s", e)
            results.append({"ticker": ticker, "side": side, "error": str(e)})

    return results


async def _execute_llm_watchlist_changes(changes: list[WatchlistChange]) -> list[dict[str, Any]]:
    """Execute watchlist changes from LLM response."""
    from ..main import get_market_source

    results = []
    for change in changes:
        ticker = change.ticker.upper()
        action = change.action.lower()
        now = datetime.now(timezone.utc).isoformat()

        try:
            with get_db() as db:
                if action == "add":
                    db.execute(
                        "INSERT OR IGNORE INTO watchlist (id, user_id, ticker, added_at) VALUES (?, 'default', ?, ?)",
                        (str(uuid.uuid4()), ticker, now),
                    )
                    source = get_market_source()
                    if source:
                        await source.add_ticker(ticker)
                    results.append({"ticker": ticker, "action": "add", "success": True})
                elif action == "remove":
                    db.execute("DELETE FROM watchlist WHERE user_id = 'default' AND ticker = ?", (ticker,))
                    source = get_market_source()
                    if source:
                        await source.remove_ticker(ticker)
                    results.append({"ticker": ticker, "action": "remove", "success": True})
        except Exception as e:
            logger.error("Watchlist change error: %s", e)
            results.append({"ticker": ticker, "action": action, "error": str(e)})

    return results


def _get_mock_response() -> LLMResponse:
    return LLMResponse(
        message="Hello! I'm FinAlly, your AI trading assistant. I can see your portfolio and help you analyze positions, suggest trades, or manage your watchlist. What would you like to do today?",
        trades=[],
        watchlist_changes=[],
    )


def _call_llm(messages: list[dict]) -> LLMResponse:
    """Call the LLM and return structured response."""
    if os.environ.get("LLM_MOCK", "").lower() == "true":
        return _get_mock_response()

    from litellm import completion

    try:
        response = completion(
            model=MODEL,
            messages=messages,
            response_format=LLMResponse,
            reasoning_effort="low",
            extra_body=EXTRA_BODY,
        )
        content = response.choices[0].message.content
        return LLMResponse.model_validate_json(content)
    except Exception as e:
        logger.error("LLM call failed: %s", e)
        raise HTTPException(status_code=502, detail=f"LLM service error: {str(e)}")


SYSTEM_PROMPT = """You are FinAlly, an AI trading assistant for a simulated trading workstation.
You help users analyze their portfolio, suggest trades, and manage their watchlist.

You ALWAYS respond with valid JSON matching this exact schema:
{
  "message": "Your conversational response (required)",
  "trades": [{"ticker": "AAPL", "side": "buy", "quantity": 10}],
  "watchlist_changes": [{"ticker": "PYPL", "action": "add"}]
}

Guidelines:
- Be concise and data-driven. Reference actual portfolio numbers.
- When the user asks to buy/sell, include the trade in the "trades" array and execute it.
- When adding/removing watchlist tickers, include in "watchlist_changes".
- This is simulated money — execute trades confidently when asked.
- Analyze P&L, concentration risk, and portfolio balance proactively.
- The "trades" and "watchlist_changes" arrays can be empty [] if no action is needed.
"""


@router.get("")
def get_chat_history() -> list[dict[str, Any]]:
    """Return recent chat history."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, role, content, actions, created_at FROM chat_messages WHERE user_id = 'default' ORDER BY created_at DESC LIMIT 100"
        ).fetchall()

    return [
        {
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "actions": json.loads(r["actions"]) if r["actions"] else None,
            "created_at": r["created_at"],
        }
        for r in reversed(rows)
    ]


@router.post("")
async def send_message(request: ChatRequest) -> dict[str, Any]:
    """Send a chat message and get an LLM response with optional trade execution."""
    user_message = request.message.strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    now = datetime.now(timezone.utc).isoformat()
    user_msg_id = str(uuid.uuid4())

    # Load conversation history and portfolio context
    with get_db() as db:
        history_rows = db.execute(
            "SELECT role, content FROM chat_messages WHERE user_id = 'default' ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        portfolio_context = _get_portfolio_context(db)

        # Save user message
        db.execute(
            "INSERT INTO chat_messages (id, user_id, role, content, created_at) VALUES (?, 'default', 'user', ?, ?)",
            (user_msg_id, user_message, now),
        )

    # Build messages for LLM
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Current portfolio state:\n{portfolio_context}"},
    ]
    for row in reversed(history_rows):
        messages.append({"role": row["role"], "content": row["content"]})
    messages.append({"role": "user", "content": user_message})

    # Call LLM
    llm_response = _call_llm(messages)

    # Execute trades and watchlist changes
    trade_results = []
    watchlist_results = []
    now_action = datetime.now(timezone.utc).isoformat()

    if llm_response.trades:
        with get_db() as db:
            trade_results = _execute_llm_trades(db, llm_response.trades, now_action)
            # Snapshot portfolio after trades
            from ..routers.portfolio import _get_portfolio_data
            portfolio = _get_portfolio_data(db)
            db.execute(
                "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) VALUES (?, 'default', ?, ?)",
                (str(uuid.uuid4()), portfolio["total_value"], now_action),
            )

    if llm_response.watchlist_changes:
        watchlist_results = await _execute_llm_watchlist_changes(llm_response.watchlist_changes)

    # Build actions object
    actions = None
    if trade_results or watchlist_results:
        actions = {
            "trades": trade_results,
            "watchlist_changes": watchlist_results,
        }

    # Save assistant response
    assistant_msg_id = str(uuid.uuid4())
    with get_db() as db:
        db.execute(
            "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) VALUES (?, 'default', 'assistant', ?, ?, ?)",
            (assistant_msg_id, llm_response.message, json.dumps(actions) if actions else None, now_action),
        )

    return {
        "id": assistant_msg_id,
        "role": "assistant",
        "content": llm_response.message,
        "actions": actions,
        "created_at": now_action,
    }
