"""Tests for the Massive data source: parsing, mode detection, snapshot and EOD polling."""

import logging
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from massive.exceptions import BadResponse

from app.market import massive_client as mc
from app.market.cache import PriceCache
from app.market.massive_client import (
    ET,
    CallBudget,
    MassiveDataSource,
    classify_error,
    previous_weekday,
    quote_from_snapshot,
)

from .fixtures import (
    SNAPSHOT_DAY_ONLY,
    SNAPSHOT_EMPTY,
    SNAPSHOT_FULL,
    SNAPSHOT_NO_TRADES,
    SNAPSHOT_OVERNIGHT,
    FakeMassiveClient,
    frozen_et,
    snap,
    snapshot_for_all,
)

NOT_ENTITLED = BadResponse(
    '{"status":"NOT_AUTHORIZED","request_id":"x","message":"You are not entitled to this data. '
    'Please upgrade your plan at https://massive.com/pricing"}'
)
RATE_LIMITED = BadResponse(
    '{"status":"ERROR","request_id":"x","error":"You\'ve exceeded the maximum requests per minute"}'
)
BAD_KEY = BadResponse('{"status":"ERROR","request_id":"x","error":"Unknown API Key"}')


def eod_ts(d: str) -> float:
    y, m, day = map(int, d.split("-"))
    return datetime(y, m, day, 16, tzinfo=ET).timestamp()


# --- Pure helpers ---


class TestQuoteFromSnapshot:
    def test_prefers_last_trade_with_ns_timestamp(self):
        q = quote_from_snapshot(snap(SNAPSHOT_FULL))
        assert q.ticker == "AAPL"
        assert q.price == 120.47
        assert q.reference_price == 119.49
        assert q.timestamp == pytest.approx(1605195918.306274, abs=1e-6)

    def test_falls_back_to_minute_bar_with_ms_timestamp(self):
        q = quote_from_snapshot(snap(SNAPSHOT_NO_TRADES))
        assert q.price == 120.44
        assert q.timestamp == pytest.approx(1605195900.0)

    def test_falls_back_to_day_close(self):
        q = quote_from_snapshot(snap(SNAPSHOT_DAY_ONLY))
        assert q.price == 120.42
        assert q.timestamp == pytest.approx(1605195918.306274, abs=1e-6)

    def test_overnight_reset_uses_previous_close(self):
        q = quote_from_snapshot(snap(SNAPSHOT_OVERNIGHT))
        assert q.price == 119.49
        assert q.reference_price == 119.49

    def test_no_data_returns_none(self):
        assert quote_from_snapshot(snap(SNAPSHOT_EMPTY)) is None

    def test_missing_ticker_returns_none(self):
        assert quote_from_snapshot(snap({**SNAPSHOT_FULL, "ticker": None})) is None

    def test_missing_trade_timestamp_uses_now(self):
        data = {**SNAPSHOT_FULL, "lastTrade": {"p": 1.5}}
        assert quote_from_snapshot(snap(data)).timestamp > 1_700_000_000


class TestClassifyError:
    def test_kinds(self):
        assert classify_error(NOT_ENTITLED) == "not_entitled"
        assert classify_error(RATE_LIMITED) == "rate_limited"
        assert classify_error(BAD_KEY) == "auth"
        assert classify_error(BadResponse("api key is invalid")) == "auth"
        assert classify_error(BadResponse('{"status":"ERROR","error":"boom"}')) == "other"


class TestPreviousWeekday:
    @pytest.mark.parametrize(
        "day,expected",
        [
            ("2026-11-11", "2026-11-10"),  # Wed -> Tue
            ("2026-11-09", "2026-11-06"),  # Mon -> Fri
            ("2026-11-08", "2026-11-06"),  # Sun -> Fri
            ("2026-11-07", "2026-11-06"),  # Sat -> Fri
        ],
    )
    def test_previous_weekday(self, day, expected):
        assert previous_weekday(date.fromisoformat(day)) == date.fromisoformat(expected)


class TestCallBudget:
    def test_limits_within_window(self):
        b = CallBudget(2, 60)
        assert b.try_acquire()
        assert b.try_acquire()
        assert not b.try_acquire()

    def test_window_slides(self, monkeypatch):
        now = {"t": 1000.0}
        monkeypatch.setattr(mc.time, "monotonic", lambda: now["t"])
        b = CallBudget(1, 60)
        assert b.try_acquire()
        now["t"] += 59
        assert not b.try_acquire()
        now["t"] += 1
        assert b.try_acquire()


# --- Snapshot mode (paid plans) ---


@pytest.fixture
def cache():
    return PriceCache()


async def started(cache, fake, tickers=("AAPL", "MSFT"), **kw) -> MassiveDataSource:
    src = MassiveDataSource("key", cache, client=fake, **kw)
    await src.start(list(tickers))
    return src


class TestSnapshotMode:
    async def test_start_detects_snapshot_and_seeds_cache(self, cache):
        fake = FakeMassiveClient(snapshot=snapshot_for_all)
        src = await started(cache, fake)
        try:
            assert src.mode == "snapshot"
            u = cache.get("MSFT")
            assert u.price == 120.47
            assert u.reference_price == 119.49
            assert u.day_change_percent == pytest.approx(0.82, abs=0.01)
            assert u.timestamp == pytest.approx(1605195918.306, abs=1e-3)
            assert fake.calls == [("snapshot", ("AAPL",)), ("snapshot", ("AAPL", "MSFT"))]
        finally:
            await src.stop()

    async def test_builds_client_without_retries(self, cache):
        with patch.object(mc, "RESTClient") as rest:
            rest.return_value = FakeMassiveClient(snapshot=snapshot_for_all)
            src = MassiveDataSource("secret", cache)
            await src.start(["AAPL"])
            await src.stop()
        rest.assert_called_once_with(api_key="secret", retries=0)

    async def test_missing_ticker_warns_once(self, cache, caplog):
        fake = FakeMassiveClient(snapshot=lambda tickers: [snap(SNAPSHOT_FULL, "AAPL")])
        src = await started(cache, fake, tickers=("AAPL", "ZZZZ"))
        with caplog.at_level(logging.WARNING):
            await src._poll_once()
            await src._poll_once()
        await src.stop()
        assert caplog.text.count("no data for ZZZZ") == 1
        assert "ZZZZ" not in cache

    async def test_malformed_snapshot_skipped(self, cache):
        bad = SimpleNamespace(ticker="BAD")  # no last_trade attribute -> AttributeError
        fake = FakeMassiveClient(snapshot=lambda t: [bad, snap(SNAPSHOT_FULL, "AAPL")])
        src = await started(cache, fake, tickers=("AAPL", "BAD"))
        await src.stop()
        assert cache.get_price("AAPL") == 120.47
        assert "BAD" not in cache

    async def test_removed_ticker_not_rewritten(self, cache):
        fake = FakeMassiveClient(snapshot=snapshot_for_all)
        src = await started(cache, fake)
        await src.remove_ticker("MSFT")
        quotes = [quote_from_snapshot(s) for s in snapshot_for_all(["AAPL", "MSFT"])]
        src._write_quotes(quotes, ["AAPL", "MSFT"])  # results of a poll that was in flight
        await src.stop()
        assert "MSFT" not in cache

    async def test_network_error_is_logged_not_raised(self, cache, caplog):
        fake = FakeMassiveClient(snapshot=snapshot_for_all)
        src = await started(cache, fake)
        fake.snapshot = ConnectionError("down")
        v = cache.version
        with caplog.at_level(logging.ERROR):
            await src._poll_once()
        await src.stop()
        assert "Massive poll failed" in caplog.text
        assert cache.version == v
        assert cache.get_price("AAPL") == 120.47  # last prices kept

    async def test_rate_limit_backs_off_and_resets(self, cache):
        fake = FakeMassiveClient(snapshot=snapshot_for_all)
        src = await started(cache, fake, poll_interval=15)
        fake.snapshot = RATE_LIMITED
        await src._poll_once()
        assert src._interval() == 30
        await src._poll_once()
        assert src._interval() == 60
        await src._poll_once()
        assert src._interval() == 60  # capped
        fake.snapshot = snapshot_for_all
        await src._poll_once()
        assert src._interval() == 15
        await src.stop()

    async def test_lost_entitlement_switches_to_eod(self, cache):
        fake = FakeMassiveClient(snapshot=snapshot_for_all)
        src = await started(cache, fake)
        fake.snapshot = NOT_ENTITLED
        await src._poll_once()
        await src.stop()
        assert src.mode == "eod"

    async def test_other_bad_response_logged(self, cache, caplog):
        fake = FakeMassiveClient(snapshot=snapshot_for_all)
        src = await started(cache, fake)
        fake.snapshot = BadResponse('{"status":"ERROR","error":"boom"}')
        with caplog.at_level(logging.ERROR):
            await src._poll_once()
        await src.stop()
        assert "Massive error" in caplog.text
        assert src.mode == "snapshot"


class TestAuthFailure:
    async def test_bad_key_logs_and_redetects_later(self, cache, caplog):
        fake = FakeMassiveClient(snapshot=BAD_KEY)
        with caplog.at_level(logging.ERROR):
            src = await started(cache, fake, eod_poll_interval=300)
        assert "rejected MASSIVE_API_KEY" in caplog.text
        assert src.mode is None
        assert src._interval() == 300
        assert len(cache) == 0
        fake.snapshot = snapshot_for_all  # key fixed upstream
        await src._poll_once()
        await src.stop()
        assert src.mode == "snapshot"
        assert cache.get_price("AAPL") == 120.47


class TestLifecycle:
    async def test_no_tickers_makes_no_calls(self, cache):
        fake = FakeMassiveClient(snapshot=snapshot_for_all)
        src = await started(cache, fake, tickers=())
        await src.stop()
        assert fake.call_count == 0
        assert src.mode is None

    async def test_poll_loop_runs_and_stop_cancels(self, cache):
        fake = FakeMassiveClient(snapshot=snapshot_for_all)
        src = await started(cache, fake, poll_interval=0.01)
        import asyncio

        await asyncio.sleep(0.05)
        await src.stop()
        n = fake.call_count
        assert n > 2
        await asyncio.sleep(0.03)
        assert fake.call_count == n
        assert src._task is None


# --- EOD mode (free plan) ---

GROUPED = {
    "2026-11-10": {"AAPL": 227.41, "MSFT": 512.00, "NVDA": 181.0},  # Tue
    "2026-11-09": {"AAPL": 225.10, "MSFT": 515.00, "NVDA": 179.0},  # Mon
}


@pytest.fixture
def at(monkeypatch):
    """at('2026-11-11 10:00') freezes 'now' (US/Eastern) inside massive_client."""

    def _set(iso: str) -> None:
        monkeypatch.setattr(mc, "datetime", frozen_et(iso))

    return _set


class TestEodMode:
    async def test_free_key_detected_and_priced(self, cache, at):
        at("2026-11-11 10:00")  # Wed, before today's bars exist
        fake = FakeMassiveClient(snapshot=NOT_ENTITLED, grouped=GROUPED)
        src = await started(cache, fake)
        await src.stop()
        assert src.mode == "eod"
        u = cache.get("AAPL")
        assert (u.price, u.reference_price) == (227.41, 225.10)
        assert u.day_change_percent == pytest.approx(1.03, abs=0.01)
        assert u.timestamp == eod_ts("2026-11-10")
        assert fake.calls == [
            ("snapshot", ("AAPL",)),
            ("grouped", "2026-11-11"),
            ("grouped", "2026-11-10"),
            ("grouped", "2026-11-09"),
        ]

    async def test_no_calls_while_data_is_fresh(self, cache, at):
        at("2026-11-11 10:00")
        fake = FakeMassiveClient(snapshot=NOT_ENTITLED, grouped=GROUPED)
        src = await started(cache, fake)
        v = cache.version
        src._budget = CallBudget(100, 60)  # budget is not what keeps these polls free
        await src._poll_once()
        await src._poll_once()
        await src.stop()
        assert fake.call_count == 4
        assert cache.version == v  # unchanged data doesn't bump the version

    async def test_new_day_rolls_reference_forward(self, cache, at):
        at("2026-11-11 10:00")
        data = dict(GROUPED)
        fake = FakeMassiveClient(snapshot=NOT_ENTITLED, grouped=data)
        src = await started(cache, fake)
        at("2026-11-11 18:00")
        src._budget = CallBudget(100, 60)  # next poll is 5 minutes later
        data["2026-11-11"] = {"AAPL": 230.00, "MSFT": 510.0}
        await src._poll_once()
        await src.stop()
        u = cache.get("AAPL")
        assert (u.price, u.reference_price) == (230.00, 227.41)
        assert u.timestamp == eod_ts("2026-11-11")
        assert fake.calls[4:] == [("grouped", "2026-11-11")]  # previous day reused, 1 call

    async def test_after_close_without_new_data_rechecks_only_today(self, cache, at):
        at("2026-11-11 10:00")
        fake = FakeMassiveClient(snapshot=NOT_ENTITLED, grouped=GROUPED)
        src = await started(cache, fake)
        at("2026-11-11 17:30")
        src._budget = CallBudget(100, 60)
        await src._poll_once()
        await src.stop()
        assert fake.calls[4:] == [("grouped", "2026-11-11")]
        assert cache.get_price("AAPL") == 227.41

    async def test_weekend_walks_back_to_friday(self, cache, at):
        at("2026-11-14 12:00")  # Saturday
        grouped = {"2026-11-13": {"AAPL": 200.0}, "2026-11-12": {"AAPL": 198.0}}
        fake = FakeMassiveClient(snapshot=NOT_ENTITLED, grouped=grouped)
        src = await started(cache, fake, tickers=("AAPL",))
        await src.stop()
        assert [c for c in fake.calls if c[0] == "grouped"] == [
            ("grouped", "2026-11-13"),
            ("grouped", "2026-11-12"),
        ]
        assert cache.get("AAPL").reference_price == 198.0

    async def test_holiday_walks_back(self, cache, at):
        at("2026-11-26 20:00")  # Thanksgiving: no bars today
        grouped = {"2026-11-25": {"AAPL": 210.0}, "2026-11-24": {"AAPL": 205.0}}
        fake = FakeMassiveClient(snapshot=NOT_ENTITLED, grouped=grouped)
        src = await started(cache, fake, tickers=("AAPL",))
        await src.stop()
        assert (cache.get_price("AAPL"), cache.get("AAPL").reference_price) == (210.0, 205.0)

    async def test_budget_exhaustion_defers_without_crashing(self, cache, at):
        at("2026-11-11 10:00")
        grouped = {"2026-11-05": {"AAPL": 190.0}, "2026-11-04": {"AAPL": 189.0}}
        fake = FakeMassiveClient(snapshot=NOT_ENTITLED, grouped=grouped)
        src = await started(cache, fake, tickers=("AAPL",))
        assert fake.call_count == MassiveDataSource.FREE_CALLS_PER_MINUTE  # probe + 3, then stop
        assert "AAPL" not in cache
        src._budget = CallBudget(100, 60)  # a minute later
        await src._poll_once()
        await src.stop()
        assert (cache.get_price("AAPL"), cache.get("AAPL").reference_price) == (190.0, 189.0)

    async def test_price_published_when_reference_fetch_is_out_of_budget(self, cache, at):
        at("2026-11-11 10:00")
        grouped = {"2026-11-10": {"AAPL": 227.41}, "2026-11-06": {"AAPL": 220.0}}
        fake = FakeMassiveClient(snapshot=NOT_ENTITLED, grouped=grouped)
        src = await started(cache, fake, tickers=("AAPL",))
        # probe, 11-11 (empty), 11-10 (data), 11-09 (empty) -> budget gone before 11-06
        assert cache.get_price("AAPL") == 227.41
        assert cache.get("AAPL").reference_price is None
        src._budget = CallBudget(100, 60)
        await src._poll_once()
        await src.stop()
        assert cache.get("AAPL").reference_price == 220.0

    async def test_add_ticker_uses_already_fetched_market_data(self, cache, at):
        at("2026-11-11 10:00")
        fake = FakeMassiveClient(snapshot=NOT_ENTITLED, grouped=GROUPED)
        src = await started(cache, fake)
        n = fake.call_count
        await src.add_ticker("nvda")
        await src.stop()
        assert cache.get_price("NVDA") == 181.0
        assert fake.call_count == n

    async def test_unknown_ticker_in_eod_is_absent(self, cache, at):
        at("2026-11-11 10:00")
        fake = FakeMassiveClient(snapshot=NOT_ENTITLED, grouped=GROUPED)
        src = await started(cache, fake, tickers=("AAPL", "ZZZZ"))
        await src.stop()
        assert "ZZZZ" not in cache
        assert "AAPL" in cache

    async def test_eod_interval_used(self, cache, at):
        at("2026-11-11 10:00")
        fake = FakeMassiveClient(snapshot=NOT_ENTITLED, grouped=GROUPED)
        src = await started(cache, fake, eod_poll_interval=300, poll_interval=15)
        await src.stop()
        assert src._interval() == 300
