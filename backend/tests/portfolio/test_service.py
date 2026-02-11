"""Tests for portfolio service."""

import tempfile
from pathlib import Path

import pytest

from app.db.database import init_db
from app.market import PriceCache
from app.portfolio.service import PortfolioService


@pytest.fixture
def setup():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(tmp.name)
    tmp.close()
    db_path.unlink()
    conn = init_db(db_path)
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    cache.update("GOOGL", 175.00)
    cache.update("TSLA", 250.00)
    service = PortfolioService(conn, cache)
    return service


class TestPortfolio:
    def test_initial_portfolio(self, setup):
        p = setup.get_portfolio()
        assert p["cash"] == 10000.0
        assert p["total_value"] == 10000.0
        assert len(p["positions"]) == 0

    def test_buy(self, setup):
        result = setup.execute_trade("AAPL", "buy", 10)
        assert result.ticker == "AAPL"
        assert result.quantity == 10
        assert result.price == 190.00
        assert result.total_cost == 1900.00
        assert result.cash_after == 8100.00

    def test_buy_updates_portfolio(self, setup):
        setup.execute_trade("AAPL", "buy", 10)
        p = setup.get_portfolio()
        assert p["cash"] == 8100.00
        assert len(p["positions"]) == 1
        assert p["positions"][0]["ticker"] == "AAPL"
        assert p["positions"][0]["quantity"] == 10

    def test_sell(self, setup):
        setup.execute_trade("AAPL", "buy", 10)
        result = setup.execute_trade("AAPL", "sell", 5)
        assert result.quantity == 5
        assert result.cash_after == 9050.00

    def test_sell_all_removes_position(self, setup):
        setup.execute_trade("AAPL", "buy", 10)
        setup.execute_trade("AAPL", "sell", 10)
        p = setup.get_portfolio()
        assert len(p["positions"]) == 0

    def test_insufficient_cash(self, setup):
        with pytest.raises(ValueError, match="Insufficient cash"):
            setup.execute_trade("AAPL", "buy", 1000)

    def test_insufficient_shares(self, setup):
        with pytest.raises(ValueError, match="Insufficient shares"):
            setup.execute_trade("AAPL", "sell", 5)

    def test_no_price(self, setup):
        with pytest.raises(ValueError, match="No price available"):
            setup.execute_trade("UNKNOWN", "buy", 1)

    def test_invalid_side(self, setup):
        with pytest.raises(ValueError, match="Side must be"):
            setup.execute_trade("AAPL", "hold", 1)

    def test_invalid_quantity(self, setup):
        with pytest.raises(ValueError, match="Quantity must be positive"):
            setup.execute_trade("AAPL", "buy", 0)

    def test_avg_cost_calculation(self, setup):
        setup.execute_trade("AAPL", "buy", 10)  # 10 @ 190
        setup.price_cache.update("AAPL", 200.00)
        setup.execute_trade("AAPL", "buy", 10)  # 10 @ 200
        p = setup.get_portfolio()
        assert p["positions"][0]["avg_cost"] == 195.00  # (1900+2000)/20

    def test_pnl_calculation(self, setup):
        setup.execute_trade("AAPL", "buy", 10)  # 10 @ 190
        setup.price_cache.update("AAPL", 200.00)
        p = setup.get_portfolio()
        pos = p["positions"][0]
        assert pos["current_price"] == 200.00
        assert pos["unrealized_pnl"] == 100.00  # (200-190)*10
        assert pos["pnl_percent"] == pytest.approx(5.26, abs=0.01)

    def test_snapshot(self, setup):
        setup.record_snapshot()
        history = setup.get_history()
        assert len(history) == 1
        assert history[0]["total_value"] == 10000.0

    def test_multiple_positions(self, setup):
        setup.execute_trade("AAPL", "buy", 5)
        setup.execute_trade("GOOGL", "buy", 5)
        p = setup.get_portfolio()
        assert len(p["positions"]) == 2
