"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging
import math
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
        stale_trade_seconds: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._stale_trade_seconds = max(stale_trade_seconds, 0.0)
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
                    price, timestamp = self._resolve_price_and_timestamp(snap)
                    if price is None:
                        logger.warning(
                            "Skipping snapshot for %s: no valid price candidates",
                            getattr(snap, "ticker", "???"),
                        )
                        continue
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

    def _resolve_price_and_timestamp(self, snap) -> tuple[float | None, float]:
        """Select best price source: fresh trade, then quote midpoint, then minute close."""
        now = time.time()

        trade_price = self._extract_trade_price(snap)
        trade_ts = self._extract_trade_timestamp(snap)
        if trade_price is not None and trade_ts is not None:
            if now - trade_ts <= self._stale_trade_seconds:
                return trade_price, trade_ts

        quote_mid = self._extract_quote_midpoint(snap)
        quote_ts = self._extract_quote_timestamp(snap)
        if quote_mid is not None and quote_ts is not None:
            return quote_mid, quote_ts

        minute_close = self._extract_minute_close(snap)
        minute_ts = self._extract_minute_timestamp(snap)
        if minute_close is not None and minute_ts is not None:
            return minute_close, minute_ts

        if trade_price is not None:
            return trade_price, trade_ts or now

        return None, now

    @staticmethod
    def _extract_trade_price(snap) -> float | None:
        trade = getattr(snap, "last_trade", None)
        return MassiveDataSource._as_positive_number(getattr(trade, "price", None))

    @staticmethod
    def _extract_quote_midpoint(snap) -> float | None:
        quote = getattr(snap, "last_quote", None)
        bid_value = MassiveDataSource._as_positive_number(getattr(quote, "bid_price", None))
        ask_value = MassiveDataSource._as_positive_number(getattr(quote, "ask_price", None))
        if bid_value is None or ask_value is None:
            return None
        return (bid_value + ask_value) / 2.0

    @staticmethod
    def _extract_minute_close(snap) -> float | None:
        minute = getattr(snap, "min", None)
        return MassiveDataSource._as_positive_number(getattr(minute, "close", None))

    @classmethod
    def _extract_trade_timestamp(cls, snap) -> float | None:
        trade = getattr(snap, "last_trade", None)
        raw_ts = cls._first_non_none(
            getattr(trade, "timestamp", None),
            getattr(trade, "sip_timestamp", None),
            getattr(trade, "participant_timestamp", None),
            getattr(trade, "trf_timestamp", None),
            getattr(snap, "updated", None),
        )
        return cls._normalize_timestamp(raw_ts)

    @classmethod
    def _extract_quote_timestamp(cls, snap) -> float | None:
        quote = getattr(snap, "last_quote", None)
        raw_ts = cls._first_non_none(
            getattr(quote, "timestamp", None),
            getattr(quote, "sip_timestamp", None),
            getattr(quote, "participant_timestamp", None),
            getattr(quote, "trf_timestamp", None),
            getattr(snap, "updated", None),
        )
        return cls._normalize_timestamp(raw_ts)

    @classmethod
    def _extract_minute_timestamp(cls, snap) -> float | None:
        minute = getattr(snap, "min", None)
        raw_ts = cls._first_non_none(
            getattr(minute, "timestamp", None),
            getattr(snap, "updated", None),
        )
        return cls._normalize_timestamp(raw_ts)

    @staticmethod
    def _first_non_none(*values):
        for value in values:
            if value is not None:
                return value
        return None

    @staticmethod
    def _normalize_timestamp(raw_ts) -> float | None:
        """Normalize snapshot timestamp values to Unix seconds."""
        ts = MassiveDataSource._as_number(raw_ts)
        if ts is None:
            return None
        # Massive timestamps may be seconds, milliseconds, microseconds, or nanoseconds.
        if ts > 1e17:  # nanoseconds
            return ts / 1e9
        if ts > 1e14:  # microseconds
            return ts / 1e6
        if ts > 1e11:  # milliseconds
            return ts / 1e3
        return ts

    @staticmethod
    def _as_number(value) -> float | None:
        if isinstance(value, bool):
            return None
        if not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        return number

    @staticmethod
    def _as_positive_number(value) -> float | None:
        number = MassiveDataSource._as_number(value)
        if number is None or number <= 0:
            return None
        return number

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
