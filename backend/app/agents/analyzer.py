"""Analyzer Agent: deterministic, read-only portfolio analysis used as LLM context."""

from __future__ import annotations

from ..db import Database
from ..market import PriceCache
from .base import BaseAgent
from .metrics import cash_pct, concentration_flags, herfindahl, position_metrics
from .models import AnalysisReport, Mover, PortfolioSummary

# Flag the portfolio as mostly idle above this cash share.
HIGH_CASH_PCT = 80.0
# Ignore tiny ticks when picking watchlist movers.
MOVER_MIN_PCT = 0.05


class AnalyzerAgent(BaseAgent):
    agent_id = "analyzer"

    def __init__(self, db: Database, cache: PriceCache, concentration_flag_pct: float) -> None:
        super().__init__(db)
        self.cache = cache
        # Same threshold the Risk agent warns at, so the two never disagree about "concentrated".
        self.concentration_flag_pct = concentration_flag_pct

    def analyze(self, summary: PortfolioSummary, watchlist: list[str]) -> AnalysisReport:
        with self.timed() as elapsed:
            report = self._analyze(summary, watchlist)
            self.log("analyze", "success", output=report, duration_ms=elapsed())
        return report

    def _analyze(self, summary: PortfolioSummary, watchlist: list[str]) -> AnalysisReport:
        metrics = position_metrics(summary)
        flags = concentration_flags(metrics, self.concentration_flag_pct)
        for m in metrics:
            upd = self.cache.get(m.ticker)
            if upd is None:
                flags.append(f"no live price for {m.ticker}; using last known price")
            else:
                # Day change here is the last-tick change from the price cache (no daily open).
                m.day_change_pct = upd.change_percent

        cash = cash_pct(summary)
        if cash > HIGH_CASH_PCT:
            flags.append(f"{cash:.0f}% of the portfolio is idle cash")

        # max/min with default=None so an empty portfolio (all cash) is handled gracefully.
        return AnalysisReport(
            total_value=summary.total_value,
            cash_pct=cash,
            positions=sorted(metrics, key=lambda m: -m.weight_pct),  # largest first
            top_concentration=max(metrics, key=lambda m: m.weight_pct, default=None),
            herfindahl_index=herfindahl(metrics),
            best_performer=max(metrics, key=lambda m: m.unrealized_pnl, default=None),
            worst_performer=min(metrics, key=lambda m: m.unrealized_pnl, default=None),
            watchlist_movers=self.movers(watchlist, {p.ticker for p in summary.positions}),
            flags=flags,
        )

    def movers(self, watchlist: list[str], held: set[str], limit: int = 3) -> list[Mover]:
        """Largest absolute % moves among watched tickers that are not held."""
        out = []
        for t in watchlist:
            if t in held:  # held tickers are covered by the positions section
                continue
            upd = self.cache.get(t)
            if upd and abs(upd.change_percent) >= MOVER_MIN_PCT:
                out.append(Mover(ticker=t, price=upd.price, change_percent=upd.change_percent))
        return sorted(out, key=lambda m: -abs(m.change_percent))[:limit]

    @staticmethod
    def to_prompt_context(report: AnalysisReport) -> str:
        lines = [
            f"Cash: {report.cash_pct:.1f}% of portfolio; concentration index (HHI): "
            f"{report.herfindahl_index:.2f}"
        ]
        if report.best_performer:
            b = report.best_performer
            lines.append(f"Best: {b.ticker} ({b.pnl_pct:+.1f}%, ${b.unrealized_pnl:+,.2f})")
        if report.worst_performer and report.worst_performer is not report.best_performer:
            w = report.worst_performer
            lines.append(f"Worst: {w.ticker} ({w.pnl_pct:+.1f}%, ${w.unrealized_pnl:+,.2f})")
        if report.watchlist_movers:
            lines.append(
                "Unheld movers: "
                + ", ".join(f"{m.ticker} {m.change_percent:+.2f}%" for m in report.watchlist_movers)
            )
        if report.flags:
            lines.append("Flags: " + "; ".join(report.flags))
        return "\n".join(lines)
