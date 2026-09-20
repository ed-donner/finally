"""Chat Orchestrator Agent: one LLM call per turn, routing its output to the other agents."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from pydantic import ValidationError

from ..db import Database
from .analyzer import AnalyzerAgent
from .base import USER_ID, BaseAgent, correlation_id, new_id, now_iso
from .models import (
    ChatResponse,
    LLMOutput,
    PortfolioSummary,
    TradeRequest,
    TradeResult,
    WatchlistEntry,
    WatchlistResult,
)
from .portfolio import PortfolioAgent
from .risk import RiskAgent
from .watchlist import WatchlistAgent

logger = logging.getLogger(__name__)

# Model + provider routing per the cerebras skill: gpt-oss-120b via OpenRouter, served by Cerebras.
MODEL = "openrouter/openai/gpt-oss-120b"
EXTRA_BODY = {"provider": {"order": ["cerebras"]}}

# How many past user/assistant exchanges are replayed into the prompt.
HISTORY_EXCHANGES = 10
# Guardrails against a runaway model: extras are dropped and reported to the user.
MAX_TRADES_PER_TURN = 10
MAX_WATCHLIST_CHANGES_PER_TURN = 10

SYSTEM_PROMPT = """You are FinAlly, an AI trading assistant inside a simulated trading workstation \
(virtual money, market orders only, instant fills at the current price, no fees).

You can:
- Analyze the user's portfolio: composition, concentration risk, cash, and P&L.
- Suggest trades with brief reasoning.
- Execute trades: include them in "trades" when the user asks for a trade or agrees to your suggestion. \
Do not put trades in the list when merely discussing or suggesting.
- Manage the watchlist: use "watchlist_changes" (action "add" or "remove"). Add tickers proactively \
when the user shows interest in them.

Rules:
- Be concise and data-driven; use the numbers in the context below, do not invent prices.
- Trades are validated by a risk system (sufficient cash for buys, sufficient shares for sells, \
position limits). Failed trades are reported back to the user automatically, so do not claim a trade \
succeeded with certainty; say you are placing it.
- Quantities may be fractional. Tickers are upper-case symbols.
- Always respond with valid JSON matching the required schema: \
{"message": str, "trades": [{"ticker", "side": "buy"|"sell", "quantity"}], \
"watchlist_changes": [{"ticker", "action": "add"|"remove"}]}. Use empty lists when there is nothing to do."""


class LLMError(Exception):
    """The LLM could not produce a usable response (missing key, provider error, bad output)."""


class OrchestratorAgent(BaseAgent):
    agent_id = "orchestrator"

    def __init__(
        self,
        db: Database,
        portfolio: PortfolioAgent,
        analyzer: AnalyzerAgent,
        risk: RiskAgent,
        watchlist: WatchlistAgent,
    ) -> None:
        super().__init__(db)
        self.portfolio = portfolio
        self.analyzer = analyzer
        self.risk = risk
        self.watchlist = watchlist

    # ------------------------------------------------------------------ pipeline

    async def handle_message(self, text: str) -> ChatResponse:
        with self.timed() as elapsed:
            # Step 1: gather context from the other agents (all read-only).
            summary = self.portfolio.get_summary()
            entries = self.watchlist.list_with_prices()
            report = self.analyzer.analyze(summary, [e.ticker for e in entries])
            history = self._load_history()
            messages = self.build_prompt(
                text, summary, self.analyzer.to_prompt_context(report), entries, history
            )

            # Step 2: the single LLM call for this turn.
            try:
                llm = await self.call_llm(messages, text)
            except LLMError as e:
                # Fail safe: nothing is executed, and the failed turn is still stored in history.
                response = ChatResponse(
                    message=f"Sorry, I couldn't get a response from the AI model ({e}). "
                    "No trades or watchlist changes were made."
                )
                self.log("handle_message", "error", input=text, error=str(e), duration_ms=elapsed())
                self.persist(text, response)
                return response

            # Step 3: route the requested actions to Risk -> Portfolio and to Watchlist.
            trade_results = await self.route_trades(llm)
            wl_results = await self.route_watchlist(llm)

            # Step 4: the model text may assume success, so append any failures explicitly.
            message = llm.message
            notes = [
                f"⚠ Could not {r.side} {r.quantity:g} {r.ticker}: {r.error}"
                for r in trade_results
                if not r.ok
            ] + [f"⚠ Watchlist: could not {r.action} {r.ticker}: {r.error}" for r in wl_results if not r.ok]
            dropped = (
                max(0, len(llm.trades) - MAX_TRADES_PER_TURN)
                + max(0, len(llm.watchlist_changes) - MAX_WATCHLIST_CHANGES_PER_TURN)
            )
            if dropped:
                notes.append(f"⚠ {dropped} requested action(s) were skipped (per-message limit).")
            if notes:
                message = message.rstrip() + "\n\n" + "\n".join(notes)

            response = ChatResponse(
                message=message,
                executed_trades=trade_results,
                watchlist_changes=wl_results,
                warnings=[w for r in trade_results for w in r.warnings],
            )
            # Step 5: store the exchange (history + executed actions) and audit it.
            self.persist(text, response)
            self.log(
                "handle_message",
                "success",
                input=text,
                output={
                    "trades": len(trade_results),
                    "watchlist_changes": len(wl_results),
                },
                duration_ms=elapsed(),
            )
        return response

    # ------------------------------------------------------------------ prompt / LLM

    def _load_history(self) -> list[dict]:
        # Newest N exchanges, then reversed below into chronological order for the prompt.
        rows = self.db.query(
            "SELECT user_message, ai_response FROM chat_messages WHERE user_id=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (USER_ID, HISTORY_EXCHANGES),
        )
        history: list[dict] = []
        for r in reversed(rows):
            history.append({"role": "user", "content": r["user_message"]})
            history.append({"role": "assistant", "content": r["ai_response"]})
        return history

    def build_prompt(
        self,
        text: str,
        summary: PortfolioSummary,
        analysis: str,
        watchlist: list[WatchlistEntry],
        history: list[dict],
    ) -> list[dict]:
        positions = (
            "\n".join(
                f"  {p.ticker}: {p.quantity:g} sh, avg ${p.avg_cost:,.2f}, now "
                f"${p.current_price or 0:,.2f}, P&L ${p.unrealized_pnl:+,.2f} ({p.pnl_percent:+.1f}%)"
                for p in summary.positions
            )
            or "  (none)"
        )
        wl = ", ".join(
            f"{e.ticker} ${e.price:,.2f}" if e.price is not None else e.ticker for e in watchlist
        )
        context = (
            "CURRENT PORTFOLIO CONTEXT\n"
            f"Cash: ${summary.cash_balance:,.2f}\n"
            f"Total value: ${summary.total_value:,.2f} (P&L ${summary.total_pnl:+,.2f})\n"
            f"Positions:\n{positions}\n"
            f"Watchlist: {wl or '(empty)'}\n"
            f"Analysis:\n{analysis}"
        )
        # Two system messages: static behavior rules, then the live per-turn context.
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": context},
            *history,
            {"role": "user", "content": text},
        ]

    async def call_llm(self, messages: list[dict], user_text: str = "") -> LLMOutput:
        """One structured-output call; a malformed response is retried once."""
        # LLM_MOCK=true short-circuits the network call (tests, demos, CI).
        if os.environ.get("LLM_MOCK", "").strip().lower() == "true":
            return mock_llm(user_text)
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise LLMError("OPENROUTER_API_KEY is not set")

        from litellm import acompletion  # imported lazily: heavy import, unused in mock mode

        last_error = "unknown error"
        for attempt in (1, 2):
            with self.timed() as elapsed:
                try:
                    resp = await acompletion(
                        model=MODEL,
                        messages=messages,
                        response_format=LLMOutput,
                        reasoning_effort="low",
                        extra_body=EXTRA_BODY,
                    )
                    content = resp.choices[0].message.content
                    # Structured output: parse and validate the JSON against the LLMOutput schema.
                    out = LLMOutput.model_validate_json(content)
                except (ValidationError, ValueError, TypeError) as e:
                    # Bad JSON / schema mismatch: worth one retry.
                    last_error = f"malformed model output: {e.__class__.__name__}"
                    self.log("llm_call", "error", error=f"attempt {attempt}: {e}", duration_ms=elapsed())
                    continue
                except Exception as e:  # noqa: BLE001 - network/auth/provider errors
                    # Not retried: auth/network problems will not fix themselves within a request.
                    self.log("llm_call", "error", error=f"attempt {attempt}: {e}", duration_ms=elapsed())
                    raise LLMError(e.__class__.__name__) from e
                self.log(
                    "llm_call",
                    "success",
                    output={"trades": len(out.trades), "watchlist_changes": len(out.watchlist_changes)},
                    duration_ms=elapsed(),
                )
                return out
        raise LLMError(last_error)

    # ------------------------------------------------------------------ routing

    async def route_trades(self, llm: LLMOutput) -> list[TradeResult]:
        """Risk-validate all trades in order against a simulated portfolio, then execute approved ones."""
        # source="chat" is stamped here; the LLM schema (TradeOrder) has no provenance field.
        reqs = [
            TradeRequest(**t.model_dump(), source="chat") for t in llm.trades[:MAX_TRADES_PER_TURN]
        ]
        # Trades on tickers outside the watchlist still need a price to validate and fill.
        await asyncio.gather(*(self.watchlist.ensure_priced(t) for t in {r.ticker for r in reqs}))
        verdicts = self.risk.validate_batch(reqs, self.portfolio.get_summary())
        # Rejected verdicts flow through execute_trade too, which turns them into ok=False results.
        return [self.portfolio.execute_trade(r, v) for r, v in zip(reqs, verdicts, strict=True)]

    async def route_watchlist(self, llm: LLMOutput) -> list[WatchlistResult]:
        # added_by='ai' records provenance in the watchlist table.
        changes = llm.watchlist_changes[:MAX_WATCHLIST_CHANGES_PER_TURN]
        return await self.watchlist.apply_changes(changes, added_by="ai")

    def persist(self, user_message: str, response: ChatResponse) -> None:
        self.db.execute(
            "INSERT INTO chat_messages (id, user_id, user_message, ai_response, executed_trades,"
            " watchlist_changes, correlation_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                new_id(),
                USER_ID,
                user_message,
                response.message,
                json.dumps([t.model_dump() for t in response.executed_trades]),
                json.dumps([w.model_dump() for w in response.watchlist_changes]),
                correlation_id.get() or None,
                now_iso(),
            ),
        )


# ---------------------------------------------------------------------- mock LLM

# Patterns for the mock LLM: 'buy 5 AAPL', 'sell 2.5 shares of TSLA', 'add PYPL to my watchlist'.
_TRADE_RE = re.compile(r"\b(buy|sell)\s+(\d+(?:\.\d+)?)\s+(?:shares?\s+of\s+)?([A-Za-z]{1,5})\b", re.I)
_WATCH_RE = re.compile(r"\b(add|remove)\s+([A-Za-z]{1,5})\b(?:\s+(?:to|from)\s+(?:my\s+)?watchlist)?", re.I)


def mock_llm(text: str) -> LLMOutput:
    """Deterministic stand-in for the LLM (LLM_MOCK=true): parses 'buy 5 AAPL' / 'add PYPL'."""
    trades, changes = [], []
    # Watchlist verbs are only honoured when 'watchlist' is mentioned, so a bare 'add' is ignored.
    for side, qty, ticker in _TRADE_RE.findall(text):
        trades.append({"ticker": ticker, "side": side.lower(), "quantity": float(qty)})
    if "watchlist" in text.lower():
        for action, ticker in _WATCH_RE.findall(text):
            changes.append({"ticker": ticker, "action": action.lower()})
    if trades or changes:
        message = "Mock mode: executing your request."
    else:
        message = "Mock mode: your portfolio looks fine. Ask me to buy, sell or manage the watchlist."
    return LLMOutput(message=message, trades=trades, watchlist_changes=changes)
