from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx

from .source import MarketDataSource
from .types import Bar

log = logging.getLogger("finally.market.massive")
BASE = "https://api.massive.com"


class MassiveSource(MarketDataSource):
    def __init__(self, api_key: str, poll_interval_seconds: float = 15.0) -> None:
        self.poll_interval_seconds = poll_interval_seconds  # free tier: 5 req/min
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = httpx.AsyncClient(base_url=BASE, timeout=10.0)

    # -- live prices: one snapshot request for all tickers -----------------

    async def get_prices(self, tickers: list[str]) -> dict[str, float]:
        if not tickers:
            return {}
        try:
            resp = await self._client.get(
                "/v2/snapshot/locale/us/markets/stocks/tickers",
                params={"tickers": ",".join(tickers)},
                headers=self._headers,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            # 429: rate limited (free tier) -> back off implicitly, keep cache.
            # 401/403: bad key. Log once; do not crash the feed.
            log.warning("Massive snapshot failed: %s", e.response.status_code)
            return {}
        except httpx.HTTPError as e:
            log.warning("Massive snapshot transport error: %s", e)
            return {}

        prices: dict[str, float] = {}
        for item in resp.json().get("tickers", []):
            # Massive may include these keys with an explicit `null` (not just
            # omit them) for halted/pre-market tickers, so `or {}` guards
            # against `.get(...)` being called on `None`.
            last = (item.get("lastTrade") or {}).get("p")
            day = (item.get("day") or {}).get("c")
            prev = (item.get("prevDay") or {}).get("c")
            price = last or day or prev  # prefer last trade; fall back when closed
            if price:
                prices[item["ticker"]] = float(price)
        return prices

    # -- history: daily aggregate bars for the detail chart ----------------

    async def get_history(self, ticker: str, days: int = 90) -> list[Bar]:
        end = date.today()
        start = end - timedelta(days=int(days * 1.5) + 5)  # pad for weekends/holidays
        try:
            resp = await self._client.get(
                f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
                params={"adjusted": "true", "sort": "asc", "limit": 5000},
                headers=self._headers,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("Massive history failed for %s: %s", ticker, e)
            return []

        results = resp.json().get("results", [])[-days:]
        return [
            Bar(t=int(r["t"]), o=float(r["o"]), h=float(r["h"]),
                low=float(r["l"]), c=float(r["c"]), v=float(r.get("v", 0.0)))
            for r in results
        ]

    async def aclose(self) -> None:
        await self._client.aclose()
