"""Chat service: LLM integration with structured outputs and auto-execution."""

import json
import os
import uuid
from datetime import datetime, timezone
from sqlite3 import Connection

import litellm
from pydantic import BaseModel

from app.market import PriceCache
from app.portfolio import PortfolioService
from app.watchlist import WatchlistService

SYSTEM_PROMPT = """You are FinAlly, an AI trading assistant for a simulated trading workstation.
You help users manage a virtual $10,000 portfolio with live market data.

Your capabilities:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with clear reasoning
- Execute trades when the user asks or agrees (buy/sell with ticker and quantity)
- Manage the watchlist (add/remove tickers)
- Be concise, data-driven, and actionable

Always respond with valid JSON matching the required schema. Include trades or watchlist_changes
only when the user requests or agrees to an action. Market orders only, instant fill at current price."""


class TradeAction(BaseModel):
    ticker: str
    side: str
    quantity: float


class WatchlistAction(BaseModel):
    ticker: str
    action: str


class LLMResponse(BaseModel):
    message: str
    trades: list[TradeAction] = []
    watchlist_changes: list[WatchlistAction] = []


MOCK_RESPONSES = [
    LLMResponse(
        message="I can see your portfolio. You have $10,000 in cash. The market is looking active today. Would you like me to suggest some trades?",
        trades=[],
        watchlist_changes=[],
    ),
    LLMResponse(
        message="Great choice! I've bought 10 shares of AAPL for you.",
        trades=[TradeAction(ticker="AAPL", side="buy", quantity=10)],
        watchlist_changes=[],
    ),
]


class ChatService:
    """Manages LLM chat with portfolio context and auto-execution."""

    def __init__(
        self, db: Connection, price_cache: PriceCache, user_id: str = "default"
    ) -> None:
        self.db = db
        self.price_cache = price_cache
        self.user_id = user_id
        self.portfolio_service = PortfolioService(db, price_cache, user_id)
        self.watchlist_service = WatchlistService(db, price_cache, user_id)

    async def send_message(self, user_message: str) -> dict:
        """Process a user message: call LLM, auto-execute actions, store history."""
        # Store user message
        self._store_message("user", user_message)

        # Get LLM response
        if os.environ.get("LLM_MOCK", "").lower() == "true":
            llm_response = self._mock_response(user_message)
        else:
            llm_response = await self._call_llm(user_message)

        # Auto-execute actions
        executed_trades = []
        trade_errors = []
        for trade in llm_response.trades:
            try:
                result = self.portfolio_service.execute_trade(
                    trade.ticker.upper(), trade.side, trade.quantity
                )
                executed_trades.append({
                    "ticker": result.ticker,
                    "side": result.side,
                    "quantity": result.quantity,
                    "price": result.price,
                })
            except ValueError as e:
                trade_errors.append(str(e))

        executed_watchlist = []
        for change in llm_response.watchlist_changes:
            try:
                if change.action == "add":
                    self.watchlist_service.add_ticker(change.ticker)
                elif change.action == "remove":
                    self.watchlist_service.remove_ticker(change.ticker)
                executed_watchlist.append({
                    "ticker": change.ticker.upper(),
                    "action": change.action,
                })
            except ValueError:
                pass

        actions = {
            "trades": executed_trades,
            "trade_errors": trade_errors,
            "watchlist_changes": executed_watchlist,
        }

        # Append errors to message if any
        message = llm_response.message
        if trade_errors:
            message += "\n\nNote: " + "; ".join(trade_errors)

        # Store assistant message
        self._store_message("assistant", message, actions)

        return {
            "message": message,
            "actions": actions,
        }

    def get_history(self, limit: int = 50) -> list[dict]:
        """Get recent chat history."""
        rows = self.db.execute(
            "SELECT role, content, actions, created_at FROM chat_messages "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (self.user_id, limit),
        ).fetchall()
        result = []
        for r in reversed(rows):
            entry = {
                "role": r["role"],
                "content": r["content"],
                "created_at": r["created_at"],
            }
            if r["actions"]:
                entry["actions"] = json.loads(r["actions"])
            result.append(entry)
        return result

    async def _call_llm(self, user_message: str) -> LLMResponse:
        """Call LLM via LiteLLM/OpenRouter with structured output."""
        context = self._build_context()
        history = self._get_recent_messages(10)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + context},
            *history,
            {"role": "user", "content": user_message},
        ]

        response = await litellm.acompletion(
            model="openrouter/openai/gpt-oss-120b",
            messages=messages,
            response_format=LLMResponse,
            api_key=os.environ.get("OPENROUTER_API_KEY"),
        )

        content = response.choices[0].message.content
        return LLMResponse.model_validate_json(content)

    def _mock_response(self, user_message: str) -> LLMResponse:
        """Return deterministic mock responses for testing."""
        msg_lower = user_message.lower()
        if "buy" in msg_lower:
            return MOCK_RESPONSES[1]
        return MOCK_RESPONSES[0]

    def _build_context(self) -> str:
        """Build portfolio context string for the LLM."""
        portfolio = self.portfolio_service.get_portfolio()
        watchlist = self.watchlist_service.get_watchlist()
        prices_str = ", ".join(
            f"{w['ticker']}: ${w['price']:.2f}" for w in watchlist if w["price"]
        )
        positions_str = ""
        if portfolio["positions"]:
            positions_str = "\n".join(
                f"  {p['ticker']}: {p['quantity']} shares @ ${p['avg_cost']:.2f} "
                f"(now ${p['current_price']:.2f}, P&L: ${p['unrealized_pnl']:.2f})"
                for p in portfolio["positions"]
            )
        else:
            positions_str = "  No positions"

        return f"""Current Portfolio:
Cash: ${portfolio['cash']:.2f}
Total Value: ${portfolio['total_value']:.2f}
Positions:
{positions_str}

Watchlist Prices: {prices_str}"""

    def _get_recent_messages(self, limit: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT role, content FROM chat_messages "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (self.user_id, limit),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def _store_message(
        self, role: str, content: str, actions: dict | None = None
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        actions_json = json.dumps(actions) if actions else None
        self.db.execute(
            "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), self.user_id, role, content, actions_json, now),
        )
        self.db.commit()
