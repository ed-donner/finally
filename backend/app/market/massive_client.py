"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging
import time

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource, normalize_ticker

logger = logging.getLogger(__name__)

# Bound how long a single REST poll may block a worker thread. Without this the
# request could hang indefinitely, and because asyncio.to_thread() can't be
# cancelled, shutdown would block on it. Keep it well under the poll interval.
DEFAULT_REQUEST_TIMEOUT = 10.0
# Hard ceiling on how long stop() waits for an in-flight poll thread to unwind.
SHUTDOWN_TIMEOUT = DEFAULT_REQUEST_TIMEOUT + 2.0


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
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._request_timeout = request_timeout
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None
        # Health: time of the last successful poll, and consecutive-failure run.
        self._last_update: float | None = None
        self._consecutive_failures = 0

    async def start(self, tickers: list[str]) -> None:
        if self._task is not None:
            raise RuntimeError("already started; call stop() before starting again")
        # Explicit connect/read timeouts so a stuck endpoint can't pin a worker
        # thread (and therefore block shutdown) indefinitely.
        self._client = RESTClient(
            api_key=self._api_key,
            connect_timeout=self._request_timeout,
            read_timeout=self._request_timeout,
        )
        self._tickers = [normalize_ticker(t) for t in tickers]

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
                # Bound the wait: a cancelled task still has to let an in-flight
                # to_thread() poll unwind, which it can't be forced to abandon.
                await asyncio.wait_for(self._task, timeout=SHUTDOWN_TIMEOUT)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                logger.warning("Massive poller did not stop within %.0fs", SHUTDOWN_TIMEOUT)
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
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    def health(self) -> dict:
        """Report feed liveness: a run of failed polls marks the source unhealthy.

        Lets the connection indicator distinguish a dead upstream (bad key,
        rate-limited, network down) from a merely-quiet market.
        """
        return {
            "healthy": self._consecutive_failures == 0,
            "last_update": self._last_update,
            "consecutive_failures": self._consecutive_failures,
        }

    # --- Internal ---

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Execute one poll cycle: fetch snapshots, update cache."""
        # Snapshot the ticker list before handing it to the worker thread, so a
        # concurrent add_ticker/remove_ticker can't mutate the list while the
        # SDK iterates it (H5: "list changed size during iteration").
        tickers = list(self._tickers)
        if not tickers or not self._client:
            return

        try:
            # The Massive RESTClient is synchronous — run in a thread to
            # avoid blocking the event loop.
            snapshots = await asyncio.to_thread(self._fetch_snapshots, tickers)
            processed = 0
            for snap in snapshots:
                try:
                    last_trade = snap.last_trade
                    price = last_trade.price
                    if price is None:
                        raise ValueError("snapshot has no last-trade price")

                    # Polygon/Massive trade timestamps are Unix NANOSECONDS
                    # (SIP, falling back to the participant feed). Convert to
                    # seconds; if absent, let the cache stamp it with now().
                    raw_ts = last_trade.sip_timestamp or last_trade.participant_timestamp
                    timestamp = raw_ts / 1_000_000_000.0 if raw_ts else None

                    # Daily open for "daily change %": today's open, falling
                    # back to the prior session's close before the market opens.
                    day_open = None
                    if snap.day is not None and snap.day.open:
                        day_open = snap.day.open
                    elif snap.prev_day is not None and snap.prev_day.close:
                        day_open = snap.prev_day.close

                    self._cache.update(
                        ticker=snap.ticker,
                        price=price,
                        timestamp=timestamp,
                        day_open=day_open,
                    )
                    processed += 1
                except (AttributeError, TypeError, ValueError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s",
                        getattr(snap, "ticker", "???"),
                        e,
                    )
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(tickers))
            # A poll that reached the API and processed at least one snapshot is
            # a healthy feed; reset the failure run and record the time.
            if processed:
                self._last_update = time.time()
                self._consecutive_failures = 0

        except Exception as e:
            self._consecutive_failures += 1
            logger.error("Massive poll failed (%d in a row): %s", self._consecutive_failures, e)
            # Don't re-raise — the loop will retry on the next interval.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.

    def _fetch_snapshots(self, tickers: list[str]) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread.

        ``tickers`` is a caller-owned snapshot of the active set, never the
        live ``self._tickers`` list, so it is safe to iterate off-thread.
        """
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=tickers,
        )
