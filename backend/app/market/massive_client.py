import asyncio
import logging
import httpx
from datetime import datetime, timezone

from .interface import MarketDataSource
from .cache import PriceCache
from .models import PriceUpdate, DailyBar

logger = logging.getLogger(__name__)

BASE_URL = "https://api.massive.com"


class MassiveClient(MarketDataSource):
    """Polls the Massive (formerly Polygon.io) REST snapshot endpoint.

    Poll interval:
      - Free tier (5 req/min): set poll_interval=15.0
      - Paid tiers (unlimited): set poll_interval=2.0–5.0
    """

    def __init__(self, api_key: str, cache: PriceCache, poll_interval: float = 15.0) -> None:
        self._api_key = api_key
        self._cache = cache
        self._poll_interval = poll_interval
        self._tickers: set[str] = set()
        self._task: asyncio.Task | None = None
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def start(self, tickers: list[str]) -> None:
        self._tickers = set(t.upper() for t in tickers)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("MassiveClient started, polling every %.1fs", self._poll_interval)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MassiveClient stopped")

    def add_ticker(self, ticker: str) -> None:
        self._tickers.add(ticker.upper())

    def remove_ticker(self, ticker: str) -> None:
        self._tickers.discard(ticker.upper())

    async def _poll_loop(self) -> None:
        async with httpx.AsyncClient(timeout=10.0) as http:
            while True:
                if self._tickers:
                    updates = await self._fetch_snapshots(http)
                    if updates:
                        await self._cache.update(updates)
                await asyncio.sleep(self._poll_interval)

    async def _fetch_snapshots(self, http: httpx.AsyncClient) -> list[PriceUpdate]:
        tickers_param = ",".join(sorted(self._tickers))
        url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers"
        try:
            response = await http.get(
                url,
                params={"tickers": tickers_param},
                headers=self._headers,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logger.warning("Rate limited by Massive API; backing off 60s")
                await asyncio.sleep(60)
            elif e.response.status_code == 403:
                logger.error("Massive API auth error (403) — check MASSIVE_API_KEY")
            else:
                logger.error("HTTP error %d from Massive API", e.response.status_code)
                await asyncio.sleep(5)
            return []
        except httpx.RequestError as e:
            logger.error("Network error polling Massive API: %s", e)
            await asyncio.sleep(5)
            return []

        return self._parse_snapshots(data)

    def _parse_snapshots(self, data: dict) -> list[PriceUpdate]:
        updates = []
        for t in data.get("tickers", []):
            try:
                last_trade = t.get("lastTrade") or {}
                price = last_trade.get("p") or t["day"]["c"]
                prev_price = t["prevDay"]["c"]
                change = price - prev_price
                change_pct = (change / prev_price * 100) if prev_price else 0.0
                ts_ns = t.get("updated", 0)
                timestamp = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
                updates.append(PriceUpdate(
                    ticker=t["ticker"],
                    price=price,
                    prev_price=prev_price,
                    timestamp=timestamp,
                    change=change,
                    change_pct=change_pct,
                ))
            except (KeyError, TypeError, ZeroDivisionError) as e:
                logger.warning("Failed to parse snapshot for %s: %s", t.get("ticker"), e)
        return updates

    async def get_daily_bars(self, ticker: str, from_date: str, to_date: str) -> list[DailyBar]:
        url = f"{BASE_URL}/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}"
        params = {"adjusted": "true", "sort": "asc", "limit": 5000}
        async with httpx.AsyncClient(timeout=15.0) as http:
            try:
                response = await http.get(url, params=params, headers=self._headers)
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, Exception) as e:
                logger.error("Failed to fetch daily bars for %s: %s", ticker, e)
                return []

        bars = []
        for r in data.get("results", []):
            dt = datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc)
            bars.append(DailyBar(
                ticker=ticker,
                date=dt.strftime("%Y-%m-%d"),
                open=r["o"],
                high=r["h"],
                low=r["l"],
                close=r["c"],
                volume=int(r["v"]),
                vwap=r.get("vw"),
            ))
        return bars
