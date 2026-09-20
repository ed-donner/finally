"""Shared Pydantic types used by the agents and the API layer."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Side = Literal["buy", "sell"]
Source = Literal["manual", "chat", "system"]

# 1-5 letters with an optional class suffix, e.g. AAPL, V, BRK.B. Applied after normalize_ticker().
TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def normalize_ticker(value: str) -> str:
    return value.strip().upper()


# --------------------------------------------------------------------------- requests
# Inbound API payloads. Tickers are normalized (trimmed, upper-cased) at parse time so every
# downstream agent can compare them directly.


class TradeOrder(BaseModel):
    """A trade as asked for by a client or the LLM. Carries no provenance (see TradeRequest)."""

    ticker: str
    side: Side
    quantity: float

    @field_validator("ticker")
    @classmethod
    def _norm(cls, v: str) -> str:
        return normalize_ticker(v)


class TradeRequest(TradeOrder):
    """A TradeOrder plus who asked for it. Set server-side, never taken from a client payload."""

    source: Source = "manual"


class WatchlistChange(BaseModel):
    """Add/remove request; used for the API body, the LLM output and internal calls."""

    ticker: str
    action: Literal["add", "remove"]

    @field_validator("ticker")
    @classmethod
    def _norm(cls, v: str) -> str:
        return normalize_ticker(v)


class ChatRequest(BaseModel):
    # Length-bounded to keep prompts (and cost) predictable.
    message: str = Field(min_length=1, max_length=4000)


# --------------------------------------------------------------------------- agent results
# What agents hand to each other and to the API layer.


class RiskVerdict(BaseModel):
    # reasons are hard failures (approved=False); warnings are advisory and never block a trade.
    approved: bool
    reasons: list[str] = []
    warnings: list[str] = []


class TradeResult(BaseModel):
    # A failed trade is still a TradeResult (ok=False, error set) so chat can report it inline.
    ok: bool
    trade_id: str | None = None
    ticker: str
    side: Side
    quantity: float
    price: float | None = None
    realized_pnl: float | None = None
    error: str | None = None
    warnings: list[str] = []


class WatchlistResult(BaseModel):
    ok: bool
    ticker: str
    action: str
    error: str | None = None
    note: str | None = None


class PositionView(BaseModel):
    # A position valued at the latest market price.
    ticker: str
    quantity: float
    avg_cost: float
    current_price: float | None
    market_value: float
    unrealized_pnl: float
    pnl_percent: float


class PortfolioSummary(BaseModel):
    # total_value = cash + positions_value; total_pnl = total_value - starting_cash.
    cash_balance: float
    positions: list[PositionView]
    positions_value: float
    total_value: float
    starting_cash: float
    unrealized_pnl: float
    realized_pnl: float
    total_pnl: float


class Snapshot(BaseModel):
    total_value: float
    cash_balance: float
    positions_value: float
    total_pnl: float
    recorded_at: str


class WatchlistEntry(BaseModel):
    ticker: str
    added_by: str
    added_at: str
    price: float | None = None
    previous_price: float | None = None
    change_percent: float | None = None
    direction: str | None = None


# --------------------------------------------------------------------------- analysis
# Output of the Analyzer and Risk agents.


class PositionMetric(BaseModel):
    ticker: str
    weight_pct: float
    unrealized_pnl: float
    pnl_pct: float
    day_change_pct: float | None = None


class Mover(BaseModel):
    ticker: str
    price: float
    change_percent: float


class AnalysisReport(BaseModel):
    total_value: float
    cash_pct: float
    positions: list[PositionMetric]
    top_concentration: PositionMetric | None
    herfindahl_index: float
    best_performer: PositionMetric | None
    worst_performer: PositionMetric | None
    watchlist_movers: list[Mover]
    flags: list[str]


class RiskLimits(BaseModel):
    # Percentages are of total portfolio value. Defaults are permissive so the $10k demo is not
    # blocked; override with RISK_<FIELD> env vars (see risk.limits_from_env).
    min_quantity: float = 0.0001
    max_order_pct: float = 100.0
    max_position_pct: float = 50.0
    warn_position_pct: float = 25.0
    min_cash_pct_warn: float = 5.0


class RiskAssessment(BaseModel):
    """Portfolio-level risk view for GET /api/risk-assessment."""

    limits: RiskLimits
    total_value: float
    cash_pct: float
    largest_position: PositionMetric | None
    herfindahl_index: float
    positions_over_warn: list[str]
    positions_over_limit: list[str]
    level: Literal["low", "moderate", "high"]
    flags: list[str]


# --------------------------------------------------------------------------- LLM I/O
# Schema requested from the model via structured outputs, plus what /api/chat returns.


class LLMOutput(BaseModel):
    """Structured output requested from the LLM (PLAN.md §9)."""

    message: str
    trades: list[TradeOrder] = []
    watchlist_changes: list[WatchlistChange] = []


class ChatResponse(BaseModel):
    message: str
    executed_trades: list[TradeResult] = []
    watchlist_changes: list[WatchlistResult] = []
    warnings: list[str] = []
