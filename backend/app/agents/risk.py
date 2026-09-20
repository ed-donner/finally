"""Risk Agent: validates trades against the portfolio and configured limits. Read-only."""

from __future__ import annotations

import math
import os

from ..db import Database
from ..market import PriceCache
from .base import EPS, BaseAgent
from .metrics import cash_pct, concentration_flags, herfindahl, position_metrics
from .models import (
    TICKER_RE,
    PortfolioSummary,
    RiskAssessment,
    RiskLimits,
    RiskVerdict,
    TradeRequest,
)


def limits_from_env() -> RiskLimits:
    """Defaults are permissive; override any field with a RISK_<FIELD> env var (e.g.
    RISK_MAX_POSITION_PCT=40). Malformed overrides are ignored."""
    overrides = {}
    for field in RiskLimits.model_fields:
        raw = os.environ.get(f"RISK_{field.upper()}")
        try:
            if raw:
                overrides[field] = float(raw)
        except ValueError:
            pass
    return RiskLimits(**overrides)


class _SimState:
    """Mutable copy of the portfolio used to validate a batch of trades in order."""

    def __init__(self, summary: PortfolioSummary) -> None:
        self.cash = summary.cash_balance
        # Trades at market price swap cash for shares 1:1, so total value stays constant
        # throughout a batch; only the split between cash and holdings changes.
        self.total_value = summary.total_value
        self.holdings = {p.ticker: p.quantity for p in summary.positions}
        self.values = {p.ticker: p.market_value for p in summary.positions}
        self.avg_costs = {p.ticker: p.avg_cost for p in summary.positions}

    def apply(self, req: TradeRequest, price: float) -> None:
        amount = req.quantity * price
        sign = 1 if req.side == "buy" else -1
        self.cash -= sign * amount
        self.holdings[req.ticker] = self.holdings.get(req.ticker, 0.0) + sign * req.quantity
        self.values[req.ticker] = self.values.get(req.ticker, 0.0) + sign * amount

    def weight_pct(self, ticker: str, extra_value: float = 0.0) -> float:
        """Weight of a ticker in total value, optionally as if `extra_value` were added to it."""
        if not self.total_value:
            return 0.0
        return (self.values.get(ticker, 0.0) + extra_value) / self.total_value * 100


class RiskAgent(BaseAgent):
    agent_id = "risk"

    def __init__(self, db: Database, cache: PriceCache, limits: RiskLimits | None = None) -> None:
        super().__init__(db)
        self.cache = cache
        self.limits = limits or limits_from_env()

    # ------------------------------------------------------------------ trade validation

    def validate_trade(self, req: TradeRequest, summary: PortfolioSummary) -> RiskVerdict:
        # A single trade is just a batch of one.
        return self.validate_batch([req], summary)[0]

    def validate_batch(
        self, reqs: list[TradeRequest], summary: PortfolioSummary
    ) -> list[RiskVerdict]:
        """Validate in order against a simulated running portfolio (approved trades are applied)."""
        state = _SimState(summary)
        verdicts = []
        for req in reqs:
            verdict = self._check(req, state)
            # Only approved trades change the simulated state, so later trades in the batch
            # see the effect of earlier ones (cumulative cash use, position build-up).
            if verdict.approved:
                state.apply(req, self.cache.get_price(req.ticker) or 0.0)
            # Every verdict is audited, approved or not.
            self.log(
                "validate_trade",
                "success" if verdict.approved else "rejected",
                input=req,
                output=verdict,
                error="; ".join(verdict.reasons) or None,
            )
            verdicts.append(verdict)
        return verdicts

    def _check(self, req: TradeRequest, state: _SimState) -> RiskVerdict:
        lim = self.limits
        reasons: list[str] = []
        warnings: list[str] = []

        # --- Basic sanity checks. All failures are collected so the user sees every problem.
        # (side is a Literal, so pydantic has already rejected anything but buy/sell.)
        if not math.isfinite(req.quantity) or req.quantity <= 0:
            reasons.append("quantity must be a positive number")
        elif req.quantity < lim.min_quantity:
            reasons.append(f"quantity must be at least {lim.min_quantity:g}")
        valid_ticker = bool(TICKER_RE.match(req.ticker))
        if not valid_ticker:
            reasons.append(f"'{req.ticker}' is not a valid ticker symbol")

        # A ticker is 'known' only if the market data source is currently pricing it.
        price = self.cache.get_price(req.ticker) if valid_ticker else None
        if valid_ticker and price is None:
            reasons.append(f"no market price available for {req.ticker}")

        # Nothing further can be checked without a valid quantity/ticker/price.
        if reasons:
            return RiskVerdict(approved=False, reasons=reasons)

        amount = req.quantity * price
        # --- Affordability: cash for buys, shares for sells.
        if req.side == "buy":
            if amount > state.cash + EPS:
                reasons.append(
                    f"insufficient cash: order costs ${amount:,.2f}, available ${state.cash:,.2f}"
                )
        else:
            held = state.holdings.get(req.ticker, 0.0)
            if held <= EPS:
                reasons.append(f"no {req.ticker} position to sell")
            elif req.quantity > held + EPS:
                reasons.append(
                    f"insufficient shares: selling {req.quantity:g} {req.ticker}, holding {held:g}"
                )

        # --- Order-size cap (as % of total portfolio value).
        if state.total_value > 0 and amount / state.total_value * 100 > lim.max_order_pct + EPS:
            reasons.append(f"order exceeds {lim.max_order_pct:g}% of portfolio value")

        # --- Concentration limits apply to buys only: judge the post-trade weight.
        if req.side == "buy" and not reasons:
            weight = state.weight_pct(req.ticker, extra_value=amount)
            if weight > lim.max_position_pct + EPS:
                reasons.append(
                    f"{req.ticker} would be {weight:.1f}% of the portfolio "
                    f"(limit {lim.max_position_pct:g}%)"
                )
            elif weight > lim.warn_position_pct:
                warnings.append(f"{req.ticker} would be {weight:.1f}% of the portfolio")
            # Soft warning: the buy leaves the portfolio nearly fully invested.
            cash_after_pct = (state.cash - amount) / state.total_value * 100 if state.total_value else 100
            if not reasons and cash_after_pct < lim.min_cash_pct_warn:
                warnings.append(f"trade leaves only {cash_after_pct:.1f}% of the portfolio in cash")

        # Soft warning only: realizing a loss is allowed but worth flagging.
        if req.side == "sell" and not reasons:
            avg_cost = state.avg_costs.get(req.ticker)
            if avg_cost is not None and price < avg_cost:
                warnings.append(
                    f"selling {req.ticker} at a loss (avg cost ${avg_cost:,.2f}, price ${price:,.2f})"
                )

        return RiskVerdict(approved=not reasons, reasons=reasons, warnings=warnings)

    # ------------------------------------------------------------------ portfolio-level view

    def assess(self, summary: PortfolioSummary) -> RiskAssessment:
        lim = self.limits
        metrics = position_metrics(summary)
        hhi = herfindahl(metrics)
        cash = cash_pct(summary)
        over_warn = [m.ticker for m in metrics if m.weight_pct > lim.warn_position_pct]
        over_limit = [m.ticker for m in metrics if m.weight_pct > lim.max_position_pct]

        flags = concentration_flags(metrics, lim.warn_position_pct)
        if metrics and cash < lim.min_cash_pct_warn:
            flags.append(f"only {cash:.1f}% cash remaining")
        if len(metrics) == 1:
            flags.append("portfolio holds a single position")

        # Overall level: hard-limit breach or a very concentrated book is 'high';
        # any warn-level position or moderate concentration is 'moderate'.
        if over_limit or hhi > 0.5:
            level = "high"
        elif over_warn or hhi > 0.3:
            level = "moderate"
        else:
            level = "low"

        result = RiskAssessment(
            limits=lim,
            total_value=summary.total_value,
            cash_pct=cash,
            largest_position=max(metrics, key=lambda m: m.weight_pct, default=None),
            herfindahl_index=hhi,
            positions_over_warn=over_warn,
            positions_over_limit=over_limit,
            level=level,
            flags=flags,
        )
        self.log("assess", "success", output=result)
        return result
