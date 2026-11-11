"""Tests for PriceUpdate."""

import dataclasses

import pytest

from app.market.models import PriceUpdate


def make(price=190.5, previous=190.0, reference=None):
    return PriceUpdate("AAPL", price, previous, timestamp=1000.0, reference_price=reference)


class TestTickChange:
    def test_up(self):
        u = make(190.5, 190.0)
        assert u.direction == "up"
        assert u.change == 0.5
        assert u.change_percent == pytest.approx(0.2632, abs=1e-4)

    def test_down(self):
        u = make(189.0, 190.0)
        assert u.direction == "down"
        assert u.change == -1.0

    def test_flat(self):
        u = make(190.0, 190.0)
        assert u.direction == "flat"
        assert u.change == 0.0
        assert u.change_percent == 0.0

    def test_zero_previous_price(self):
        assert make(10.0, 0.0).change_percent == 0.0


class TestDayChange:
    def test_with_reference(self):
        u = make(price=209.0, reference=190.0)
        assert u.day_change == 19.0
        assert u.day_change_percent == 10.0

    def test_negative(self):
        assert make(price=171.0, reference=190.0).day_change_percent == -10.0

    @pytest.mark.parametrize("reference", [None, 0.0])
    def test_unknown_reference(self, reference):
        u = make(reference=reference)
        assert u.day_change is None
        assert u.day_change_percent is None


class TestValueObject:
    def test_immutable(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            make().price = 1.0  # type: ignore[misc]

    def test_default_timestamp(self):
        assert PriceUpdate("AAPL", 1.0, 1.0).timestamp > 1_600_000_000

    def test_default_reference_is_none(self):
        assert PriceUpdate("AAPL", 1.0, 1.0).reference_price is None

    def test_to_dict(self):
        assert make(190.5, 190.0, reference=180.0).to_dict() == {
            "ticker": "AAPL",
            "price": 190.5,
            "previous_price": 190.0,
            "timestamp": 1000.0,
            "change": 0.5,
            "change_percent": pytest.approx(0.2632, abs=1e-4),
            "direction": "up",
            "reference_price": 180.0,
            "day_change": 10.5,
            "day_change_percent": 5.83,
        }

    def test_to_dict_without_reference(self):
        d = make().to_dict()
        assert d["reference_price"] is None
        assert d["day_change"] is None
        assert d["day_change_percent"] is None
