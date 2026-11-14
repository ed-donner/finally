"""Test doubles and real Massive JSON samples for market data tests.

Snapshots are built with the real SDK models (TickerSnapshot.from_dict), so attribute
names and timestamp units are checked against the actual `massive` package rather than
hand-written mocks.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from massive.rest.models import GroupedDailyAgg, TickerSnapshot

from app.market.massive_client import ET

# Shapes taken from the Massive docs (Full Market Snapshot sample)
SNAPSHOT_FULL: dict[str, Any] = {
    "ticker": "AAPL",
    "todaysChange": 0.98,
    "todaysChangePerc": 0.82,
    "updated": 1605195918306274000,  # ns
    "day": {"o": 119.62, "h": 120.53, "l": 118.81, "c": 120.42, "v": 28727868, "vw": 119.725},
    "prevDay": {"o": 117.19, "h": 119.63, "l": 116.44, "c": 119.49, "v": 110597265, "vw": 118.4998},
    "min": {"o": 120.43, "h": 120.47, "l": 120.37, "c": 120.44, "v": 270796, "t": 1605195900000},
    "lastTrade": {"p": 120.47, "s": 236, "x": 10, "i": "4046", "t": 1605195918306274000},
    "lastQuote": {"p": 120.46, "s": 8, "P": 120.47, "S": 4, "t": 1605195918507251700},
}
SNAPSHOT_NO_TRADES = {k: v for k, v in SNAPSHOT_FULL.items() if k not in ("lastTrade", "lastQuote")}
SNAPSHOT_DAY_ONLY = {k: v for k, v in SNAPSHOT_NO_TRADES.items() if k != "min"}
SNAPSHOT_OVERNIGHT = {
    "ticker": "AAPL",
    "day": {"o": 0, "h": 0, "l": 0, "c": 0, "v": 0},
    "min": {"c": 0},
    "prevDay": {"c": 119.49},
    "updated": 0,
}
SNAPSHOT_EMPTY = {"ticker": "AAPL"}


def snap(d: dict[str, Any], ticker: str | None = None) -> TickerSnapshot:
    data = dict(d)
    if ticker:
        data["ticker"] = ticker
    return TickerSnapshot.from_dict(data)


def grouped(rows: dict[str, float]) -> list[GroupedDailyAgg]:
    return [
        GroupedDailyAgg.from_dict({"T": t, "o": c, "h": c, "l": c, "c": c, "v": 1000, "t": 1})
        for t, c in rows.items()
    ]


class FakeMassiveClient:
    """Stand-in for massive.RESTClient. Records every call.

    snapshot: list of TickerSnapshot | callable(tickers) -> list | Exception (raised)
    grouped:  {"YYYY-MM-DD": {ticker: close}} | Exception (raised); missing date -> []
    """

    def __init__(self, snapshot: Any = None, grouped: Any = None) -> None:
        self.snapshot = snapshot
        self.grouped = grouped if grouped is not None else {}
        self.calls: list[tuple[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def get_snapshot_all(self, market_type, tickers=None, **kwargs):
        self.calls.append(("snapshot", tuple(tickers or ())))
        s = self.snapshot
        if isinstance(s, Exception):
            raise s
        if callable(s):
            return s(tickers)
        return list(s or [])

    def get_grouped_daily_aggs(self, date, adjusted=None, **kwargs):
        self.calls.append(("grouped", date))
        g = self.grouped
        if isinstance(g, Exception):
            raise g
        return grouped(g.get(date, {}))


def snapshot_for_all(tickers):
    """Snapshot callable: a full snapshot for every requested ticker."""
    return [snap(SNAPSHOT_FULL, t) for t in tickers]


def frozen_et(iso: str) -> type[datetime]:
    """A datetime subclass whose now() returns `iso` interpreted in US/Eastern."""
    fixed = datetime.fromisoformat(iso).replace(tzinfo=ET)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D401
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    return FrozenDatetime


class FakeRequest:
    """Minimal Request for the SSE generator: disconnects on the Nth is_disconnected() call."""

    class _Client:
        host = "127.0.0.1"

    def __init__(self, disconnect_after: int) -> None:
        self._remaining = disconnect_after
        self.client = self._Client()

    async def is_disconnected(self) -> bool:
        self._remaining -= 1
        return self._remaining <= 0
