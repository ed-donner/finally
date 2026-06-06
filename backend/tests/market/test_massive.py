"""Tests for MassiveDataSource (mocked)."""

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import app.market.massive_client as massive_client
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource

# A realistic SIP timestamp in NANOSECONDS (2024-02-10T15:20:00Z),
# i.e. 1707578400 seconds.
SIP_TS_NS = 1_707_578_400_000_000_000
SIP_TS_SECONDS = 1_707_578_400.0


def _make_snapshot(
    ticker: str,
    price: float,
    sip_timestamp_ns: int = SIP_TS_NS,
    day_open: float | None = None,
    prev_close: float | None = None,
) -> MagicMock:
    """Create a mock Massive snapshot mirroring the real TickerSnapshot shape.

    The real ``LastTrade`` model has no ``timestamp`` attribute — it exposes
    ``sip_timestamp`` / ``participant_timestamp`` in nanoseconds. ``day`` and
    ``prev_day`` are ``Agg`` objects carrying ``open`` / ``close``.
    """
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade = MagicMock()
    snap.last_trade.price = price
    snap.last_trade.sip_timestamp = sip_timestamp_ns
    snap.last_trade.participant_timestamp = sip_timestamp_ns
    snap.day = MagicMock()
    snap.day.open = day_open
    snap.prev_day = MagicMock()
    snap.prev_day.close = prev_close
    return snap


@pytest.mark.asyncio
class TestMassiveDataSource:
    """Unit tests for MassiveDataSource with mocked API."""

    async def test_poll_updates_cache(self):
        """Test that polling updates the cache."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,  # Long interval so the loop doesn't auto-poll
        )
        source._tickers = ["AAPL", "GOOGL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        mock_snapshots = [
            _make_snapshot("AAPL", 190.50),
            _make_snapshot("GOOGL", 175.25),
        ]

        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_malformed_snapshot_skipped(self):
        """Test that malformed snapshots are skipped gracefully."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL", "BAD"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        good_snap = _make_snapshot("AAPL", 190.50)
        bad_snap = MagicMock()
        bad_snap.ticker = "BAD"
        bad_snap.last_trade = None  # Will cause AttributeError

        with patch.object(source, "_fetch_snapshots", return_value=[good_snap, bad_snap]):
            await source._poll_once()

        # Good ticker processed, bad one skipped
        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_api_error_does_not_crash(self):
        """Test that API errors don't crash the poller."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()  # Should not raise

        assert cache.get_price("AAPL") is None  # No update happened

    async def test_timestamp_conversion(self):
        """SIP timestamps are converted from nanoseconds to seconds."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        mock_snapshots = [_make_snapshot("AAPL", 190.50, sip_timestamp_ns=SIP_TS_NS)]

        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        update = cache.get("AAPL")
        assert update is not None
        assert update.timestamp == SIP_TS_SECONDS  # nanoseconds → seconds

    async def test_participant_timestamp_fallback(self):
        """Falls back to participant_timestamp when sip_timestamp is missing."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        snap = _make_snapshot("AAPL", 190.50)
        snap.last_trade.sip_timestamp = None
        snap.last_trade.participant_timestamp = SIP_TS_NS

        with patch.object(source, "_fetch_snapshots", return_value=[snap]):
            await source._poll_once()

        assert cache.get("AAPL").timestamp == SIP_TS_SECONDS

    async def test_day_open_from_snapshot(self):
        """day_open is taken from the snapshot's day.open for daily change %."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        mock_snapshots = [_make_snapshot("AAPL", 195.00, day_open=190.00)]

        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        update = cache.get("AAPL")
        assert update.day_open == 190.00
        assert update.day_change_percent == pytest.approx(2.6316, abs=1e-4)

    async def test_day_open_falls_back_to_prev_close(self):
        """Before the open, prev_day.close stands in for the day open."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        # day.open is None (pre-market); prev_day.close should be used.
        mock_snapshots = [_make_snapshot("AAPL", 195.00, day_open=None, prev_close=189.00)]

        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        assert cache.get("AAPL").day_open == 189.00

    async def test_add_ticker(self):
        """Test adding a ticker."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("AAPL")
        assert "AAPL" in source.get_tickers()

    async def test_add_ticker_uppercase_normalization(self):
        """Test that tickers are normalized to uppercase."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("aapl")
        assert "AAPL" in source.get_tickers()

    async def test_add_ticker_strips_whitespace(self):
        """Test that ticker whitespace is stripped."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("  AAPL  ")
        assert "AAPL" in source.get_tickers()

    async def test_remove_ticker(self):
        """Test removing a ticker."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = ["AAPL", "GOOGL"]
        cache.update("AAPL", 190.00)

        await source.remove_ticker("AAPL")
        assert "AAPL" not in source.get_tickers()
        assert cache.get("AAPL") is None

    async def test_get_tickers(self):
        """Test getting the list of active tickers."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = ["AAPL", "GOOGL"]

        tickers = source.get_tickers()
        assert tickers == ["AAPL", "GOOGL"]

    async def test_empty_tickers_skips_poll(self):
        """Test that polling is skipped when there are no tickers."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = []

        # Should not call _fetch_snapshots
        with patch.object(source, "_fetch_snapshots") as mock_fetch:
            await source._poll_once()
            mock_fetch.assert_not_called()

    async def test_stop_is_idempotent(self):
        """Test that stop() can be called multiple times."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.stop()
        await source.stop()  # Should not raise

    async def test_stop_cancels_task(self):
        """Test that stop() cancels the polling task."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=10.0)

        # Mock the client and start
        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", return_value=[]):
                await source.start(["AAPL"])

        # Verify task is running
        assert source._task is not None
        assert not source._task.done()

        # Stop and verify task is cancelled
        await source.stop()
        assert source._task is None

    async def test_start_immediate_poll(self):
        """Test that start() does an immediate poll before starting the loop."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)

        mock_snapshots = [_make_snapshot("AAPL", 190.50)]

        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
                await source.start(["AAPL"])

        # Cache should have data immediately from the first poll
        assert cache.get_price("AAPL") == 190.50

        await source.stop()

    async def test_double_start_raises(self):
        """start() twice raises rather than leaking the first poller (L5)."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", return_value=[]):
                await source.start(["AAPL"])
                with pytest.raises(RuntimeError):
                    await source.start(["GOOGL"])
        await source.stop()

    async def test_start_normalizes_tickers(self):
        """start() normalizes its ticker list like add_ticker does (M6)."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", return_value=[]):
                await source.start(["aapl", "  googl "])
        assert source.get_tickers() == ["AAPL", "GOOGL"]
        await source.stop()

    async def test_add_ticker_during_poll_is_safe(self):
        """Mutating the watchlist during an in-flight poll can't corrupt it (H5).

        _fetch_snapshots receives a private copy of the ticker list, so adds
        landing mid-poll neither raise 'list changed size during iteration' nor
        get lost.
        """
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        def slow_fetch(tickers):
            # Iterate the received list slowly while the caller mutates
            # self._tickers underneath us.
            out = []
            for t in tickers:
                time.sleep(0.001)
                out.append(_make_snapshot(t, 100.0))
            return out

        with patch.object(source, "_fetch_snapshots", side_effect=slow_fetch):
            poll = asyncio.create_task(source._poll_once())
            for i in range(50):
                await source.add_ticker(f"T{i}")
                await asyncio.sleep(0)
            await poll  # must not raise

        # The in-flight poll used the pre-mutation snapshot (just AAPL).
        assert cache.get_price("AAPL") == 100.0
        assert "T49" in source.get_tickers()

    async def test_stop_is_bounded_when_poll_hangs(self, monkeypatch):
        """stop() returns promptly even if a poll thread is stuck (M5).

        to_thread() can't be cancelled, so without the wait_for bound stop()
        would block for the full request. Here the 'request' sleeps far longer
        than the (patched-small) shutdown ceiling, yet stop() still returns.
        """
        monkeypatch.setattr(massive_client, "SHUTDOWN_TIMEOUT", 0.2)
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=0.01)
        source._client = MagicMock()
        source._tickers = ["AAPL"]

        hang_started = threading.Event()

        def hang(tickers):
            hang_started.set()
            time.sleep(1.0)
            return []

        with patch.object(source, "_fetch_snapshots", side_effect=hang):
            source._task = asyncio.create_task(source._poll_loop(), name="massive-poller")
            # Wait until the worker thread is actually inside the hang.
            await asyncio.to_thread(hang_started.wait, 1.0)
            t0 = time.monotonic()
            await source.stop()
            elapsed = time.monotonic() - t0

        assert elapsed < 0.9  # bounded by SHUTDOWN_TIMEOUT, not the 1.0s sleep
        assert source._task is None

    async def test_health_tracks_failures_and_recovery(self):
        """health() flips unhealthy on a failed poll and recovers on success (N2)."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        assert source.health()["healthy"] is True  # nothing has failed yet

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("boom")):
            await source._poll_once()
        h = source.health()
        assert h["healthy"] is False
        assert h["consecutive_failures"] == 1

        with patch.object(source, "_fetch_snapshots", return_value=[_make_snapshot("AAPL", 190.5)]):
            await source._poll_once()
        h = source.health()
        assert h["healthy"] is True
        assert h["consecutive_failures"] == 0
        assert h["last_update"] is not None
