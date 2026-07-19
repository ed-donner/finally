"""Prompt construction for the FinAlly chat assistant."""

from __future__ import annotations

import json

SYSTEM_PROMPT = """\
You are FinAlly, an AI trading assistant embedded in a simulated trading workstation.
The user trades a virtual portfolio with fake money — stakes are zero.

Your job:
- Analyze portfolio composition, risk concentration, and P&L.
- Suggest trades with clear, data-driven reasoning.
- Execute trades when the user asks or agrees (market orders, instant fill).
- Manage the watchlist proactively when it helps.
- Be concise and quantitative.

You MUST respond with valid JSON matching this schema:
{
  "message": "<conversational reply to the user>",
  "trades": [{"ticker": "AAPL", "side": "buy", "quantity": 10}],
  "watchlist_changes": [{"ticker": "PYPL", "action": "add"}]
}
Omit trades / watchlist_changes (or use empty arrays) when no action is needed.
Every trade you list will be auto-executed, so only include trades you intend to place.
"""


def _portfolio_context(portfolio: dict, watchlist: list[dict]) -> str:
    positions = portfolio.get("positions", [])
    lines = [
        f"Cash balance: ${portfolio.get('cash_balance', 0):,.2f}",
        f"Total portfolio value: ${portfolio.get('total_value', 0):,.2f}",
        f"Total unrealized P&L: ${portfolio.get('total_unrealized_pl', 0):,.2f}",
    ]
    if positions:
        lines.append("Positions:")
        for p in positions:
            lines.append(
                f"  {p['ticker']}: {p['quantity']} @ avg ${p['avg_cost']:.2f}, "
                f"now ${p['current_price']:.2f}, P&L ${p['unrealized_pl']:.2f} "
                f"({p['unrealized_pl_pct']:.2f}%)"
            )
    else:
        lines.append("Positions: none")

    if watchlist:
        wl = ", ".join(
            f"{w['ticker']}"
            + (f" ${w['price']:.2f}" if w.get("price") is not None else "")
            for w in watchlist
        )
        lines.append(f"Watchlist: {wl}")
    return "\n".join(lines)


def build_messages(
    user_message: str,
    portfolio: dict,
    watchlist: list[dict],
    history: list[dict],
) -> list[dict]:
    """Assemble the chat message list for the LLM call."""
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "system",
            "content": "Current portfolio context:\n" + _portfolio_context(portfolio, watchlist),
        },
    ]
    for msg in history:
        role = msg.get("role")
        if role in {"user", "assistant"}:
            messages.append({"role": role, "content": msg.get("content", "")})
    messages.append({"role": "user", "content": user_message})
    return messages


def context_summary(portfolio: dict, watchlist: list[dict]) -> str:
    """Compact JSON context — handy for logging/tests."""
    return json.dumps(
        {"portfolio": portfolio, "watchlist": [w["ticker"] for w in watchlist]},
        default=str,
    )
