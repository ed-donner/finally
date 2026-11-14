"""Tests for app.services.portfolio."""

from __future__ import annotations

import pytest

from app import db
from app.services.portfolio import (
    TradeError,
    execute_trade,
    get_history,
    get_portfolio,
    record_portfolio_snapshot,
)

pytestmark = pytest.mark.usefixtures("temp_db")


class TestBuy:
    def test_buy_happy_path(self, cache):
        trade = execute_trade("aapl", "BUY", 10, cache)
        assert trade["ticker"] == "AAPL"
        assert trade["side"] == "buy"
        assert trade["quantity"] == 10
        assert trade["price"] == 100.0
        assert trade["id"] and trade["executed_at"]
        assert db.get_cash_balance() == pytest.approx(9000.0)
        pos = db.get_position("AAPL")
        assert pos["quantity"] == 10
        assert pos["avg_cost"] == 100.0
        assert len(db.get_trades()) == 1

    def test_avg_cost_weighted(self, cache):
        execute_trade("AAPL", "buy", 10, cache)
        cache.update("AAPL", 130.0)
        execute_trade("AAPL", "buy", 5, cache)
        pos = db.get_position("AAPL")
        assert pos["quantity"] == 15
        assert pos["avg_cost"] == pytest.approx((10 * 100 + 5 * 130) / 15)
        assert db.get_cash_balance() == pytest.approx(10000 - 1000 - 650)

    def test_fractional_shares(self, cache):
        execute_trade("AAPL", "buy", 0.5, cache)
        assert db.get_position("AAPL")["quantity"] == pytest.approx(0.5)
        assert db.get_cash_balance() == pytest.approx(9950.0)

    def test_insufficient_cash(self, cache):
        with pytest.raises(TradeError, match="Insufficient cash"):
            execute_trade("NVDA", "buy", 21, cache)  # 10_500 > 10_000
        assert db.get_cash_balance() == 10000.0
        assert db.get_position("NVDA") is None
        assert db.get_trades() == []

    def test_buy_exactly_all_cash(self, cache):
        execute_trade("NVDA", "buy", 20, cache)
        assert db.get_cash_balance() == pytest.approx(0.0)


class TestSell:
    def test_partial_sell_keeps_avg_cost(self, cache):
        execute_trade("AAPL", "buy", 10, cache)
        cache.update("AAPL", 120.0)
        trade = execute_trade("AAPL", "sell", 4, cache)
        assert trade["side"] == "sell"
        assert trade["price"] == 120.0
        pos = db.get_position("AAPL")
        assert pos["quantity"] == 6
        assert pos["avg_cost"] == 100.0
        assert db.get_cash_balance() == pytest.approx(9000 + 480)

    def test_sell_at_loss(self, cache):
        execute_trade("AAPL", "buy", 10, cache)
        cache.update("AAPL", 80.0)
        execute_trade("AAPL", "sell", 10, cache)
        assert db.get_cash_balance() == pytest.approx(9800.0)

    def test_selling_all_removes_position(self, cache):
        execute_trade("AAPL", "buy", 10, cache)
        execute_trade("AAPL", "sell", 10, cache)
        assert db.get_position("AAPL") is None
        assert get_portfolio(cache)["positions"] == []

    def test_fractional_sell_to_zero_removes_position(self, cache):
        execute_trade("AAPL", "buy", 0.3, cache)
        execute_trade("AAPL", "sell", 0.1, cache)
        execute_trade("AAPL", "sell", 0.2, cache)  # float residue < epsilon
        assert db.get_position("AAPL") is None

    def test_oversell(self, cache):
        execute_trade("AAPL", "buy", 5, cache)
        with pytest.raises(TradeError, match="Insufficient shares"):
            execute_trade("AAPL", "sell", 6, cache)
        assert db.get_position("AAPL")["quantity"] == 5

    def test_sell_without_position(self, cache):
        with pytest.raises(TradeError, match="Insufficient shares"):
            execute_trade("AAPL", "sell", 1, cache)


class TestValidation:
    @pytest.mark.parametrize("side", ["hold", "", None, 3])
    def test_invalid_side(self, cache, side):
        with pytest.raises(TradeError, match="Side"):
            execute_trade("AAPL", side, 1, cache)

    @pytest.mark.parametrize("qty", [0, -1, float("nan"), float("inf"), "abc"])
    def test_invalid_quantity(self, cache, qty):
        with pytest.raises(TradeError, match="Quantity"):
            execute_trade("AAPL", "buy", qty, cache)

    @pytest.mark.parametrize("ticker", ["", "1ABC", "TOO-LONG-TICKER", "A B", "$$"])
    def test_invalid_ticker(self, cache, ticker):
        with pytest.raises(TradeError, match="Invalid ticker"):
            execute_trade(ticker, "buy", 1, cache)

    def test_no_price(self, cache):
        with pytest.raises(TradeError, match="No price"):
            execute_trade("ZZZZ", "buy", 1, cache)


class TestPortfolio:
    def test_empty_portfolio(self, cache):
        p = get_portfolio(cache)
        assert p == {
            "cash_balance": 10000.0,
            "positions_value": 0.0,
            "total_value": 10000.0,
            "unrealized_pnl": 0.0,
            "unrealized_pnl_percent": 0.0,
            "positions": [],
        }

    def test_valuation(self, cache):
        execute_trade("AAPL", "buy", 10, cache)
        cache.update("AAPL", 110.0)
        p = get_portfolio(cache)
        assert p["cash_balance"] == 9000.0
        assert p["positions_value"] == 1100.0
        assert p["total_value"] == 10100.0
        assert p["unrealized_pnl"] == 100.0
        assert p["unrealized_pnl_percent"] == 10.0
        (pos,) = p["positions"]
        assert pos == {
            "ticker": "AAPL",
            "quantity": 10.0,
            "avg_cost": 100.0,
            "current_price": 110.0,
            "market_value": 1100.0,
            "unrealized_pnl": 100.0,
            "unrealized_pnl_percent": 10.0,
            "weight": round(1100 / 10100, 4),
        }

    def test_current_price_falls_back_to_avg_cost(self, cache):
        execute_trade("AAPL", "buy", 10, cache)
        cache.remove("AAPL")
        (pos,) = get_portfolio(cache)["positions"]
        assert pos["current_price"] == 100.0
        assert pos["unrealized_pnl"] == 0.0


class TestSnapshots:
    def test_trade_records_snapshot(self, cache):
        execute_trade("AAPL", "buy", 10, cache)
        execute_trade("AAPL", "sell", 5, cache)
        history = get_history()
        assert len(history) == 2
        assert all(h["total_value"] == 10000.0 for h in history)

    def test_record_snapshot(self, cache):
        execute_trade("AAPL", "buy", 10, cache)
        cache.update("AAPL", 150.0)
        snap = record_portfolio_snapshot(cache)
        assert snap["total_value"] == 10500.0
        assert get_history()[-1]["total_value"] == 10500.0
        assert set(get_history()[-1]) == {"total_value", "recorded_at"}
