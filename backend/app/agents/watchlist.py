"""Watchlist Agent: owns the watchlist table and keeps the market data source in sync."""

from __future__ import annotations

import os

from ..db import Database
from ..market import MarketDataSource, PriceCache
from .base import USER_ID, BaseAgent, new_id, now_iso
from .models import (
    TICKER_RE,
    WatchlistChange,
    WatchlistEntry,
    WatchlistResult,
    normalize_ticker,
)


def _max_watchlist() -> int:
    """Upper bound on watched tickers (each costs a price-stream / API-poll slot).

    Read on use, not at import, so a value set in .env (loaded by create_app) is honoured.
    """
    try:
        return int(os.environ.get("MAX_WATCHLIST", "50"))
    except ValueError:
        return 50


class WatchlistAgent(BaseAgent):
    agent_id = "watchlist"

    def __init__(self, db: Database, cache: PriceCache, source: MarketDataSource) -> None:
        super().__init__(db)
        self.cache = cache
        self.source = source

    # ------------------------------------------------------------------ reads

    def _rows(self) -> list[dict]:
        """The single watchlist query, so ordering and filtering cannot drift between readers."""
        return self.db.query(
            "SELECT ticker, added_by, added_at FROM watchlist WHERE user_id=?"
            " ORDER BY added_at, ticker",
            (USER_ID,),
        )

    def tickers(self) -> list[str]:
        """Watchlist tickers only."""
        return [r["ticker"] for r in self._rows()]

    def tracked_tickers(self) -> list[str]:
        """Union of watchlist and held positions: what the market data source must price."""
        # Positions must keep being priced even if the ticker leaves the watchlist.
        held = [
            r["ticker"]
            for r in self.db.query("SELECT ticker FROM positions WHERE user_id=?", (USER_ID,))
        ]
        return sorted(set(self.tickers()) | set(held))

    def list_with_prices(self) -> list[WatchlistEntry]:
        out = []
        for r in self._rows():
            upd = self.cache.get(r["ticker"])
            prices = (
                {
                    "price": upd.price,
                    "previous_price": upd.previous_price,
                    "change_percent": upd.change_percent,
                    "direction": upd.direction,
                }
                if upd
                else {}
            )
            out.append(WatchlistEntry(**r, **prices))
        return out

    # ------------------------------------------------------------------ writes

    def _result(
        self,
        action: str,
        ticker: str,
        ok: bool,
        error: str | None = None,
        note: str | None = None,
    ) -> WatchlistResult:
        """Build the result and audit-log it, so every outcome is returned and recorded alike."""
        result = WatchlistResult(ok=ok, ticker=ticker, action=action, error=error, note=note)
        self.log(action, "success" if ok else "rejected", input=ticker, output=result, error=error)
        return result

    async def add(self, ticker: str, added_by: str = "user") -> WatchlistResult:
        ticker = normalize_ticker(ticker)
        if not TICKER_RE.match(ticker):
            return self._result("add", ticker, False, f"'{ticker}' is not a valid ticker symbol")
        # Adding is idempotent: an existing entry is reported as success, not an error.
        if self.db.query_one(
            "SELECT 1 FROM watchlist WHERE user_id=? AND ticker=?", (USER_ID, ticker)
        ):
            return self._result("add", ticker, True, note="already on the watchlist")
        count = self.db.query_one(
            "SELECT COUNT(*) AS n FROM watchlist WHERE user_id=?", (USER_ID,)
        )["n"]
        if count >= _max_watchlist():
            return self._result("add", ticker, False, f"watchlist is full ({_max_watchlist()} tickers)")

        # Start pricing the ticker first so it has a quote as soon as it appears in the list.
        await self.source.add_ticker(ticker)
        self.db.execute(
            "INSERT INTO watchlist (id, user_id, ticker, added_at, added_by) VALUES (?,?,?,?,?)",
            (new_id(), USER_ID, ticker, now_iso(), added_by),
        )
        return self._result("add", ticker, True)

    async def remove(self, ticker: str) -> WatchlistResult:
        ticker = normalize_ticker(ticker)
        cur = self.db.execute(
            "DELETE FROM watchlist WHERE user_id=? AND ticker=?", (USER_ID, ticker)
        )
        if cur.rowcount == 0:
            return self._result("remove", ticker, False, f"{ticker} is not on the watchlist")
        # Held tickers must keep streaming prices so the portfolio stays valued.
        if not self.db.query_one(
            "SELECT 1 FROM positions WHERE user_id=? AND ticker=?", (USER_ID, ticker)
        ):
            await self.source.remove_ticker(ticker)
        return self._result("remove", ticker, True)

    async def apply_changes(
        self, changes: list[WatchlistChange], added_by: str = "user"
    ) -> list[WatchlistResult]:
        """Apply a batch; one failure does not block the rest."""
        out = []
        for c in changes:
            out.append(
                await (self.add(c.ticker, added_by) if c.action == "add" else self.remove(c.ticker))
            )
        return out

    async def ensure_priced(self, ticker: str) -> None:
        """Make sure the market source is pricing a ticker about to be traded.

        Lets a trade on a ticker that is not on the watchlist get a price. It does not add the
        ticker to the watchlist.
        """
        ticker = normalize_ticker(ticker)
        if TICKER_RE.match(ticker) and ticker not in self.cache:
            await self.source.add_ticker(ticker)
