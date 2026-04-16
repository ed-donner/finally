"""Tests for PriceUpdate dataclass."""

import pytest

from app.market.models import PriceUpdate


class TestPriceUpdate:
    """Unit tests for the PriceUpdate model."""

    def test_price_update_creation(self):
        update = PriceUpdate(
            ticker="AAPL", price=190.50, prev_price=190.00, open_price=189.00, timestamp=1234567890.0
        )
        assert update.ticker == "AAPL"
        assert update.price == 190.50
        assert update.prev_price == 190.00
        assert update.open_price == 189.00
        assert update.timestamp == 1234567890.0

    def test_change_calculation(self):
        update = PriceUpdate(
            ticker="AAPL", price=190.50, prev_price=190.00, open_price=190.00, timestamp=1234567890.0
        )
        assert update.change == 0.50

    def test_change_negative(self):
        update = PriceUpdate(
            ticker="AAPL", price=189.50, prev_price=190.00, open_price=190.00, timestamp=1234567890.0
        )
        assert update.change == -0.50

    def test_change_percent_up(self):
        update = PriceUpdate(
            ticker="AAPL", price=190.00, prev_price=100.00, open_price=100.00, timestamp=1234567890.0
        )
        assert update.change_percent == 90.0

    def test_change_percent_down(self):
        update = PriceUpdate(
            ticker="AAPL", price=100.00, prev_price=200.00, open_price=200.00, timestamp=1234567890.0
        )
        assert update.change_percent == -50.0

    def test_change_percent_zero_previous(self):
        update = PriceUpdate(
            ticker="AAPL", price=100.00, prev_price=0.00, open_price=0.00, timestamp=1234567890.0
        )
        assert update.change_percent == 0.0

    def test_direction_up(self):
        update = PriceUpdate(
            ticker="AAPL", price=191.00, prev_price=190.00, open_price=190.00, timestamp=1234567890.0
        )
        assert update.direction == "up"

    def test_direction_down(self):
        update = PriceUpdate(
            ticker="AAPL", price=189.00, prev_price=190.00, open_price=190.00, timestamp=1234567890.0
        )
        assert update.direction == "down"

    def test_direction_flat(self):
        update = PriceUpdate(
            ticker="AAPL", price=190.00, prev_price=190.00, open_price=190.00, timestamp=1234567890.0
        )
        assert update.direction == "flat"

    def test_to_dict_fields(self):
        update = PriceUpdate(
            ticker="AAPL", price=190.50, prev_price=190.00, open_price=189.00, timestamp=1234567890.0
        )
        result = update.to_dict()

        assert result["ticker"] == "AAPL"
        assert result["price"] == 190.50
        assert result["prev_price"] == 190.00
        assert result["open_price"] == 189.00
        assert result["change"] == 0.50
        assert result["change_percent"] == 0.2632  # (0.50 / 190.00) * 100
        assert result["direction"] == "up"

    def test_to_dict_timestamp_is_iso_string(self):
        """to_dict() must return an ISO 8601 UTC string, not a Unix float."""
        update = PriceUpdate(
            ticker="AAPL", price=190.00, prev_price=190.00, open_price=190.00, timestamp=1234567890.0
        )
        result = update.to_dict()
        ts = result["timestamp"]
        assert isinstance(ts, str)
        assert ts.endswith("Z")
        assert "T" in ts
        # 1234567890 UTC = 2009-02-13T23:31:30Z
        assert ts == "2009-02-13T23:31:30Z"

    def test_immutability(self):
        update = PriceUpdate(
            ticker="AAPL", price=190.50, prev_price=190.00, open_price=190.00, timestamp=1234567890.0
        )
        with pytest.raises(AttributeError):
            update.price = 200.00
