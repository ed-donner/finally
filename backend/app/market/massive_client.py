"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource, normalize_ticker

logger = logging.getLogger(__name__)

_NS_PER_SECOND = 1_000_000_000


def _positive(value: Any) -> float | None:
    """Return value as a float if it is a finite number > 0, else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value) and value > 0:
            return float(value)
    return None


def extract_price(snap: Any) -> tuple[float, float | None] | None:
    """Pick (price, unix_seconds) from a massive TickerSnapshot.

    Priority:
      1. last_trade.price, timestamped by last_trade.sip_timestamp (nanoseconds)
      2. day.close (if > 0), timestamped by snap.updated (nanoseconds)
      3. prev_day.close (if > 0), timestamped by snap.updated (nanoseconds)

    The timestamp is None when no usable one exists (the cache then uses now).
    Returns None if the snapshot has no usable price.
    """
    updated = _positive(getattr(snap, "updated", None))
    fallback_ts = updated / _NS_PER_SECOND if updated is not None else None

    trade = getattr(snap, "last_trade", None)
    if trade is not None:
        price = _positive(getattr(trade, "price", None))
        if price is not None:
            sip = _positive(getattr(trade, "sip_timestamp", None))
            return price, (sip / _NS_PER_SECOND if sip is not None else fallback_ts)

    for bar in (getattr(snap, "day", None), getattr(snap, "prev_day", None)):
        if bar is not None:
            price = _positive(getattr(bar, "close", None))
            if price is not None:
                return price, fallback_ts

    return None


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all tracked
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
        # Normalize and de-duplicate (preserving order), keeping any early adds
        self._tickers = list(dict.fromkeys(normalize_ticker(t) for t in [*tickers, *self._tickers]))

        # Do an immediate first poll so the cache has data right away
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval",
            len(self._tickers),
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
        ticker = normalize_ticker(ticker)
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if ticker in self._tickers:
            self._tickers.remove(ticker)
            logger.info("Massive: removed ticker %s", ticker)
        self._cache.remove(ticker)

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
        tickers = list(self._tickers)  # copy: the worker thread must not see mutations
        if not tickers or not self._client:
            return

        try:
            # The Massive RESTClient is synchronous — run in a thread to
            # avoid blocking the event loop.
            snapshots = await asyncio.to_thread(self._fetch_snapshots, tickers)
        except Exception as e:
            # Don't re-raise — the loop will retry on the next interval.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.
            logger.error("Massive poll failed: %s", e)
            return

        # Re-read the tracked set: a ticker removed while the request was in
        # flight must not be written back into the cache.
        tracked = set(self._tickers)
        processed = 0
        for snap in snapshots:
            ticker = getattr(snap, "ticker", None)
            if ticker not in tracked:
                continue
            extracted = extract_price(snap)
            if extracted is None:
                logger.warning("Skipping snapshot for %s: no usable price", ticker)
                continue
            price, timestamp = extracted
            self._cache.update(ticker=ticker, price=price, timestamp=timestamp)
            processed += 1
        logger.debug("Massive poll: updated %d/%d tickers", processed, len(tickers))

    def _fetch_snapshots(self, tickers: list[str]) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        assert self._client is not None
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=tickers,
        )
