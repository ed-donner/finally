"""System prompt, portfolio context and message assembly for the chat assistant."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.market import PriceCache

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 20

SYSTEM_PROMPT = """You are FinAlly, an AI trading assistant inside a simulated trading workstation.
The user trades a simulated portfolio with virtual money (market orders only, instant fill at
the current price, no fees, fractional shares allowed).

Your job:
- Analyze the portfolio: composition, risk concentration, diversification and P&L.
- Suggest trades with brief, data-driven reasoning.
- Execute trades when the user asks for them or agrees to your suggestion, by listing them in
  `trades`. Trades execute automatically - do not ask for confirmation once the user asked.
- Manage the watchlist proactively via `watchlist_changes` (add tickers the user is interested
  in, remove ones they ask to drop).
- Be concise and data-driven. Use numbers from the portfolio context below; never invent prices.
- Only trade what is affordable (cash) or held (shares). Buys need enough cash at the current
  price; sells need enough shares.

Always respond with a single JSON object with exactly these keys:
{"message": "<your reply to the user>",
 "trades": [{"ticker": "AAPL", "side": "buy" | "sell", "quantity": <number > 0>}],
 "watchlist_changes": [{"ticker": "PYPL", "action": "add" | "remove"}]}
Use empty lists when there is nothing to execute. Do not describe a trade as done in `message`
unless it is in `trades`; the system reports execution results to the user separately."""


def _round(value: Any, ndigits: int = 2) -> Any:
    return round(value, ndigits) if isinstance(value, (int, float)) else value


def build_portfolio_context(price_cache: PriceCache) -> str:
    """Human/LLM-readable snapshot of cash, positions with P&L, and watchlist prices."""
    # Imported lazily so this module can be imported without the service layer.
    from app.services.portfolio import get_portfolio
    from app.services.watchlist import get_watchlist_with_prices

    sections: list[str] = []
    try:
        p = get_portfolio(price_cache)
        sections.append(
            "Portfolio:\n"
            f"- Cash: ${_round(p.get('cash_balance'))}\n"
            f"- Positions value: ${_round(p.get('positions_value'))}\n"
            f"- Total value: ${_round(p.get('total_value'))}\n"
            f"- Unrealized P&L: ${_round(p.get('unrealized_pnl'))} "
            f"({_round(p.get('unrealized_pnl_percent'))}%)"
        )
        positions = p.get("positions") or []
        if positions:
            lines = [
                f"- {pos.get('ticker')}: {pos.get('quantity')} sh @ avg ${_round(pos.get('avg_cost'))}, "
                f"now ${_round(pos.get('current_price'))}, value ${_round(pos.get('market_value'))}, "
                f"P&L ${_round(pos.get('unrealized_pnl'))} ({_round(pos.get('unrealized_pnl_percent'))}%), "
                f"weight {_round((pos.get('weight') or 0) * 100, 1)}%"
                for pos in positions
            ]
            sections.append("Positions:\n" + "\n".join(lines))
        else:
            sections.append("Positions: none (all cash)")
    except Exception:
        logger.exception("Failed to build portfolio context")
        sections.append("Portfolio: unavailable")

    try:
        items = get_watchlist_with_prices(price_cache)
        lines = []
        for it in items:
            price = it.get("price")
            if price is None:
                lines.append(f"- {it.get('ticker')}: price not yet available")
                continue
            day = it.get("day_change_percent")
            day_txt = f", day {_round(day)}%" if day is not None else ""
            lines.append(f"- {it.get('ticker')}: ${_round(price)}{day_txt}")
        sections.append("Watchlist:\n" + ("\n".join(lines) if lines else "- (empty)"))
    except Exception:
        logger.exception("Failed to build watchlist context")
        sections.append("Watchlist: unavailable")

    return "\n\n".join(sections)


def _history_content(msg: dict) -> str:
    """Assistant history includes a compact record of executed actions so the model
    knows what actually happened."""
    content = msg.get("content") or ""
    actions = msg.get("actions")
    if msg.get("role") == "assistant" and isinstance(actions, dict):
        summary = {
            k: actions.get(k) for k in ("trades", "watchlist_changes") if actions.get(k)
        }
        if summary:
            content += "\n[Executed actions: " + json.dumps(summary, separators=(",", ":")) + "]"
    return content


def build_messages(
    user_message: str, context: str, history: list[dict]
) -> list[dict[str, str]]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "Current portfolio context:\n\n" + context},
    ]
    for msg in history[-HISTORY_LIMIT:]:
        if msg.get("role") in ("user", "assistant"):
            messages.append({"role": msg["role"], "content": _history_content(msg)})
    messages.append({"role": "user", "content": user_message})
    return messages
