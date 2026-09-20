"""Concentration maths shared by the Analyzer and Risk agents (one definition, one threshold)."""

from __future__ import annotations

from .models import PortfolioSummary, PositionMetric


def position_metrics(summary: PortfolioSummary) -> list[PositionMetric]:
    """One metric per position, weights as % of total portfolio value."""
    total = summary.total_value
    return [
        PositionMetric(
            ticker=p.ticker,
            weight_pct=(p.market_value / total * 100) if total else 0.0,
            unrealized_pnl=p.unrealized_pnl,
            pnl_pct=p.pnl_percent,
        )
        for p in summary.positions
    ]


def herfindahl(metrics: list[PositionMetric]) -> float:
    """Sum of squared weights: 1.0 = a single position, ~0 = well diversified."""
    return sum((m.weight_pct / 100) ** 2 for m in metrics)


def cash_pct(summary: PortfolioSummary) -> float:
    """Cash as % of total value; an empty (zero-value) portfolio counts as all cash."""
    return summary.cash_balance / summary.total_value * 100 if summary.total_value else 100.0


def concentration_flags(metrics: list[PositionMetric], threshold_pct: float) -> list[str]:
    return [
        f"{m.ticker} is {m.weight_pct:.0f}% of the portfolio"
        for m in metrics
        if m.weight_pct > threshold_pct
    ]
