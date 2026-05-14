"""Unit tests for market data models."""
from datetime import datetime, timezone
import pytest
from app.market.models import PriceUpdate, DailyBar


def make_update(change: float) -> PriceUpdate:
    return PriceUpdate(
        ticker="AAPL",
        price=100.0 + change,
        prev_price=100.0,
        timestamp=datetime.now(tz=timezone.utc),
        change=change,
        change_pct=change,
    )


class TestPriceUpdateDirection:
    def test_up_when_positive_change(self):
        assert make_update(1.0).direction == "up"

    def test_down_when_negative_change(self):
        assert make_update(-1.0).direction == "down"

    def test_flat_when_zero_change(self):
        assert make_update(0.0).direction == "flat"

    def test_direction_ignores_change_pct(self):
        # direction is solely based on change, not change_pct
        u = PriceUpdate(
            ticker="X",
            price=50.0,
            prev_price=100.0,
            timestamp=datetime.now(tz=timezone.utc),
            change=-50.0,
            change_pct=0.0,  # misleadingly zero pct
        )
        assert u.direction == "down"


class TestPriceUpdateFields:
    def test_all_fields_stored(self):
        ts = datetime.now(tz=timezone.utc)
        u = PriceUpdate(
            ticker="TSLA",
            price=250.50,
            prev_price=248.00,
            timestamp=ts,
            change=2.50,
            change_pct=1.008,
        )
        assert u.ticker == "TSLA"
        assert u.price == 250.50
        assert u.prev_price == 248.00
        assert u.timestamp is ts
        assert u.change == 2.50
        assert u.change_pct == 1.008


class TestDailyBar:
    def test_all_fields_stored(self):
        bar = DailyBar(
            ticker="AAPL",
            date="2024-01-15",
            open=189.0,
            high=191.5,
            low=188.5,
            close=190.0,
            volume=50_000_000,
            vwap=190.12,
        )
        assert bar.ticker == "AAPL"
        assert bar.date == "2024-01-15"
        assert bar.open == 189.0
        assert bar.vwap == 190.12

    def test_vwap_defaults_to_none(self):
        bar = DailyBar(
            ticker="MSFT",
            date="2024-01-15",
            open=415.0,
            high=420.0,
            low=413.0,
            close=418.0,
            volume=20_000_000,
        )
        assert bar.vwap is None
