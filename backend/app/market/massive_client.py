"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging
import time

from massive import RESTClient

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call, then writes results to the PriceCache.

    Rate limits:
      - Free tier: 5 req/min → poll every 15s (default)
      - Paid tiers: higher limits → poll every 2-5s
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)

        # Do an immediate first poll so the cache has data right away
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval",
            len(tickers),
            self._interval,
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internal ---

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Execute one poll cycle: fetch snapshots, update cache."""
        if not self._tickers or not self._client:
            return

        try:
            # The Massive RESTClient is synchronous — run in a thread to
            # avoid blocking the event loop.
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    timestamp = self._extract_snapshot_timestamp(snap)
                    day_baseline = self._extract_day_baseline(snap, price)
                    self._cache.update(
                        ticker=snap.ticker,
                        price=price,
                        timestamp=timestamp,
                        day_baseline_price=day_baseline,
                    )
                    processed += 1
                except (AttributeError, TypeError, ValueError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s",
                        getattr(snap, "ticker", "???"),
                        e,
                    )
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — the loop will retry on the next interval.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_snapshot_all(
            # Some massive client versions mis-handle enum objects here and
            # construct a bad URL. Use the literal API value for compatibility.
            market_type="stocks",
            tickers=self._tickers,
        )

    @staticmethod
    def _extract_snapshot_timestamp(snap) -> float:
        """Return snapshot timestamp in Unix seconds across SDK schema variants."""
        trade = getattr(snap, "last_trade", None)

        raw_ts = getattr(trade, "timestamp", None)
        if raw_ts is None:
            raw_ts = getattr(trade, "sip_timestamp", None)
        if raw_ts is None:
            raw_ts = getattr(trade, "participant_timestamp", None)
        if raw_ts is None:
            raw_ts = getattr(trade, "trf_timestamp", None)
        if raw_ts is None:
            raw_ts = getattr(snap, "updated", None)

        if raw_ts is None:
            return time.time()

        ts = float(raw_ts)
        # Massive timestamps may be seconds, milliseconds, microseconds, or nanoseconds.
        if ts > 1e17:  # nanoseconds
            return ts / 1e9
        if ts > 1e14:  # microseconds
            return ts / 1e6
        if ts > 1e11:  # milliseconds
            return ts / 1e3
        return ts

    @staticmethod
    def _extract_day_baseline(snap, price: float) -> float | None:
        """Prefer current day open; fallback to previous close or derived value."""
        day = getattr(snap, "day", None)
        day_open = getattr(day, "open", None)
        if day_open is not None:
            day_open = float(day_open)
            if day_open > 0:
                return day_open

        prev_day = getattr(snap, "prev_day", None)
        prev_close = getattr(prev_day, "close", None)
        if prev_close is not None:
            prev_close = float(prev_close)
            if prev_close > 0:
                return prev_close

        todays_change = getattr(snap, "todays_change", None)
        if todays_change is not None:
            baseline = float(price) - float(todays_change)
            if baseline > 0:
                return baseline

        return None
