"""Massive (formerly Polygon.io) data source.

Two modes, auto-detected at start-up:

  snapshot (paid plans)  GET /v2/snapshot/locale/us/markets/stocks/tickers  - one call per poll
                         for all tracked tickers; real-time or 15-min delayed.
  eod      (free plan)   GET /v2/aggs/grouped/locale/us/market/stocks/{date} - whole-market
                         daily closes in one call; free keys get end-of-day data only and
                         5 requests/minute, and are not entitled to the snapshot endpoint.

See planning/MASSIVE_API.md and planning/MARKET_DATA_DESIGN.md section 9.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from massive import RESTClient
from massive.exceptions import BadResponse
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource, normalize_ticker

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
Mode = Literal["snapshot", "eod"]
ErrorKind = Literal["not_entitled", "rate_limited", "auth", "other"]


# --- Pure helpers (no network) ---


@dataclass(frozen=True, slots=True)
class Quote:
    """Provider-neutral parsed price, ready for PriceCache.update()."""

    ticker: str
    price: float
    timestamp: float  # Unix seconds
    reference_price: float | None


def quote_from_snapshot(snap) -> Quote | None:
    """TickerSnapshot -> Quote.

    Price fallback chain: last_trade.price -> min.close -> day.close -> prev_day.close.
    lastTrade is absent on plans without trade data; day/min are zeroed by the nightly reset.
    Timestamp units: last_trade.sip_timestamp and updated are ns; min.timestamp is ms.
    """
    ticker = getattr(snap, "ticker", None)
    if not ticker:
        return None
    last_trade, minute, day, prev = snap.last_trade, snap.min, snap.day, snap.prev_day
    ref = prev.close if prev and prev.close else None

    if last_trade and last_trade.price:
        ts = last_trade.sip_timestamp / 1e9 if last_trade.sip_timestamp else time.time()
        return Quote(ticker, last_trade.price, ts, ref)
    if minute and minute.close:
        ts = minute.timestamp / 1e3 if minute.timestamp else time.time()
        return Quote(ticker, minute.close, ts, ref)
    if day and day.close:
        ts = snap.updated / 1e9 if snap.updated else time.time()
        return Quote(ticker, day.close, ts, ref)
    if ref:
        return Quote(ticker, ref, time.time(), ref)
    return None


def classify_error(exc: Exception) -> ErrorKind:
    """massive.exceptions.BadResponse carries only the response body; classify by content."""
    text = str(exc).lower()
    if "not_authorized" in text or "not entitled" in text or "upgrade your plan" in text:
        return "not_entitled"
    if "exceeded the maximum requests" in text or "too many requests" in text:
        return "rate_limited"
    if "unknown api key" in text or ("api key" in text and "invalid" in text):
        return "auth"
    return "other"


def previous_weekday(d: date) -> date:
    """The closest Mon-Fri strictly before d."""
    d -= timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d


class CallBudget:
    """Non-blocking sliding-window limiter (free plan: 5 calls / 60 s)."""

    def __init__(self, max_calls: int, per_seconds: float) -> None:
        self._max = max_calls
        self._per = per_seconds
        self._calls: deque[float] = deque()

    def try_acquire(self) -> bool:
        now = time.monotonic()
        while self._calls and now - self._calls[0] >= self._per:
            self._calls.popleft()
        if len(self._calls) >= self._max:
            return False
        self._calls.append(now)
        return True


class BudgetExhaustedError(Exception):
    """Raised when the free-plan call budget is used up; the poll resumes next interval."""


# --- Data source ---


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive REST API."""

    FREE_CALLS_PER_MINUTE = 4  # one below the 5/min limit, for safety
    MAX_BACKOFF = 60.0
    EOD_PUBLISH_HOUR_ET = 17  # daily bars are complete some time after the 16:00 close
    MAX_LOOKBACK_DAYS = 5  # weekdays to walk back over holidays

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
        eod_poll_interval: float = 300.0,
        client: RESTClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._snapshot_interval = poll_interval
        self._eod_interval = eod_poll_interval
        self._client = client
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._mode: Mode | None = None
        self._backoff: float | None = None
        self._warned_missing: set[str] = set()
        # EOD state
        self._budget = CallBudget(self.FREE_CALLS_PER_MINUTE, 60.0)
        self._eod_date: date | None = None
        self._eod_closes: dict[str, float] = {}
        self._ref_date: date | None = None
        self._ref_closes: dict[str, float] = {}

    # --- Lifecycle ---

    async def start(self, tickers: list[str]) -> None:
        if self._client is None:
            # retries=0: the SDK retries 429s after 0.1 s, which would burn a free quota instantly
            self._client = RESTClient(api_key=self._api_key, retries=0)
        self._tickers = list(dict.fromkeys(normalize_ticker(t) for t in tickers if t.strip()))
        await self._poll_once()  # detects mode and seeds the cache
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info("Massive source started: mode=%s, %d tickers", self._mode, len(self._tickers))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Massive source stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if not ticker or ticker in self._tickers:
            return
        self._tickers.append(ticker)
        if self._mode == "eod":
            self._write_eod(ticker)  # data already fetched for the whole market
        logger.info("Massive: added %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    @property
    def mode(self) -> Mode | None:
        """'snapshot', 'eod', or None if not yet detected (or the key was rejected)."""
        return self._mode

    # --- Polling ---

    def _interval(self) -> float:
        if self._backoff:
            return self._backoff
        return self._eod_interval if self._mode == "eod" else self._snapshot_interval

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval())
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers or self._client is None:
            return
        tickers = list(self._tickers)  # the list may change while the thread call runs
        try:
            if self._mode is None:
                self._mode = await asyncio.to_thread(self._detect_mode, tickers[0])
                logger.info("Massive mode: %s", self._mode)
            if self._mode == "snapshot":
                quotes = await asyncio.to_thread(self._fetch_snapshot, tickers)
                self._write_quotes(quotes, tickers)
            else:
                try:
                    await asyncio.to_thread(self._refresh_eod)
                finally:
                    # publish whatever we have, even if the budget ran out mid-refresh
                    for t in list(self._tickers):
                        self._write_eod(t)
            self._backoff = None
        except BudgetExhaustedError:
            logger.debug("Massive: free-plan call budget exhausted; continuing next poll")
        except BadResponse as e:
            self._handle_bad_response(e)
        except Exception:
            logger.exception("Massive poll failed")

    def _handle_bad_response(self, e: BadResponse) -> None:
        kind = classify_error(e)
        if kind == "rate_limited":
            self._backoff = min((self._backoff or self._interval()) * 2, self.MAX_BACKOFF)
            logger.warning("Massive rate limited; next poll in %.0fs", self._backoff)
        elif kind == "not_entitled" and self._mode == "snapshot":
            logger.warning("Massive plan has no snapshot access; switching to EOD mode")
            self._mode = "eod"
        elif kind == "auth":
            logger.error("Massive rejected MASSIVE_API_KEY (%s). Fix the key and restart.", e)
            self._mode = None  # re-detect on a later poll
            self._backoff = self._eod_interval
        else:
            logger.error("Massive error: %s", e)

    # --- Mode detection (thread) ---

    def _detect_mode(self, probe_ticker: str) -> Mode:
        try:
            self._client.get_snapshot_all(SnapshotMarketType.STOCKS, tickers=[probe_ticker])
            return "snapshot"
        except BadResponse as e:
            if classify_error(e) == "not_entitled":
                self._budget.try_acquire()  # the probe counted against the free quota
                return "eod"
            raise

    # --- Snapshot mode (thread) ---

    def _fetch_snapshot(self, tickers: list[str]) -> list[Quote]:
        snaps = self._client.get_snapshot_all(SnapshotMarketType.STOCKS, tickers=tickers)
        quotes: list[Quote] = []
        for snap in snaps or []:
            try:
                quote = quote_from_snapshot(snap)
            except (AttributeError, TypeError) as e:
                logger.warning(
                    "Skipping malformed snapshot for %s: %s", getattr(snap, "ticker", "?"), e
                )
                continue
            if quote:
                quotes.append(quote)
        return quotes

    def _write_quotes(self, quotes: list[Quote], requested: list[str]) -> None:
        live = set(self._tickers)  # ignore tickers removed while the poll was in flight
        seen: set[str] = set()
        for q in quotes:
            if q.ticker in live:
                self._cache.update(
                    q.ticker, q.price, q.timestamp, reference_price=q.reference_price
                )
                seen.add(q.ticker)
        for t in set(requested) - seen:
            if t in live and t not in self._warned_missing:
                logger.warning("Massive returned no data for %s (invalid ticker?)", t)
                self._warned_missing.add(t)

    # --- EOD mode (thread) ---

    def _grouped(self, d: date) -> dict[str, float]:
        if not self._budget.try_acquire():
            raise BudgetExhaustedError
        aggs = self._client.get_grouped_daily_aggs(d.isoformat(), adjusted=True)
        return {a.ticker: a.close for a in (aggs or []) if a.ticker and a.close}

    def _latest_with_data(
        self, start: date, floor: date | None = None
    ) -> tuple[date, dict[str, float]] | None:
        """Walk back over weekdays from `start` (inclusive) to the first date with data."""
        d = start if start.weekday() < 5 else previous_weekday(start)
        for _ in range(self.MAX_LOOKBACK_DAYS):
            if floor is not None and d <= floor:
                return None
            closes = self._grouped(d)
            if closes:
                return d, closes
            d = previous_weekday(d)
        return None

    def _refresh_eod(self) -> None:
        now = datetime.now(ET)
        today = now.date()
        # Nothing newer can exist: we already have today, or have the last weekday's data
        # and today's bars aren't published yet.
        fresh = self._eod_date is not None and (
            self._eod_date == today
            or (self._eod_date >= previous_weekday(today) and now.hour < self.EOD_PUBLISH_HOUR_ET)
        )
        if not fresh:
            found = self._latest_with_data(today, floor=self._eod_date)
            if found:
                if self._eod_date is not None and found[0] > self._eod_date:
                    # the old "latest" day becomes the reference day at no extra cost
                    self._ref_date, self._ref_closes = self._eod_date, self._eod_closes
                self._eod_date, self._eod_closes = found
        if self._eod_date is not None and (
            self._ref_date is None or self._ref_date >= self._eod_date
        ):
            ref = self._latest_with_data(previous_weekday(self._eod_date))
            if ref:
                self._ref_date, self._ref_closes = ref

    def _write_eod(self, ticker: str) -> None:
        price = self._eod_closes.get(ticker)
        if price is None or self._eod_date is None:
            return
        ts = datetime(
            self._eod_date.year, self._eod_date.month, self._eod_date.day, 16, tzinfo=ET
        ).timestamp()
        ref = self._ref_closes.get(ticker)
        current = self._cache.get(ticker)
        if (
            current is not None
            and current.price == round(price, 2)
            and current.timestamp == ts
            and current.reference_price == (round(ref, 2) if ref else None)
        ):
            return  # unchanged -> don't bump the cache version
        self._cache.update(ticker, price, ts, reference_price=ref)
