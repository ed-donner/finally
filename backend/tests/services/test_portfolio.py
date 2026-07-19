"""Trade execution + portfolio valuation tests."""

import pytest

from app.db import repo
from app.services.portfolio import TradeError, build_portfolio, execute_trade


def test_buy_reduces_cash_and_creates_position(env):
    execute_trade("AAPL", "buy", 10, 100.0)
    assert repo.get_profile()["cash_balance"] == pytest.approx(9000.0)
    pos = repo.get_position("AAPL")
    assert pos["quantity"] == pytest.approx(10)
    assert pos["avg_cost"] == pytest.approx(100.0)


def test_buy_insufficient_cash_raises(env):
    with pytest.raises(TradeError, match="Insufficient cash"):
        execute_trade("AAPL", "buy", 1000, 100.0)  # 100k > 10k
    assert repo.get_profile()["cash_balance"] == pytest.approx(10000.0)


def test_weighted_average_cost(env):
    execute_trade("AAPL", "buy", 10, 100.0)
    execute_trade("AAPL", "buy", 10, 200.0)
    pos = repo.get_position("AAPL")
    assert pos["quantity"] == pytest.approx(20)
    assert pos["avg_cost"] == pytest.approx(150.0)


def test_sell_adds_cash_and_reduces_position(env):
    execute_trade("AAPL", "buy", 10, 100.0)
    execute_trade("AAPL", "sell", 4, 120.0)
    pos = repo.get_position("AAPL")
    assert pos["quantity"] == pytest.approx(6)
    assert pos["avg_cost"] == pytest.approx(100.0)  # unchanged on sell
    # 10000 - 1000 (buy) + 480 (sell) = 9480
    assert repo.get_profile()["cash_balance"] == pytest.approx(9480.0)


def test_sell_all_deletes_position(env):
    execute_trade("AAPL", "buy", 5, 100.0)
    execute_trade("AAPL", "sell", 5, 100.0)
    assert repo.get_position("AAPL") is None


def test_sell_more_than_owned_raises(env):
    execute_trade("AAPL", "buy", 5, 100.0)
    with pytest.raises(TradeError, match="Insufficient shares"):
        execute_trade("AAPL", "sell", 10, 100.0)


def test_invalid_side_and_quantity(env):
    with pytest.raises(TradeError):
        execute_trade("AAPL", "hold", 1, 100.0)
    with pytest.raises(TradeError):
        execute_trade("AAPL", "buy", 0, 100.0)


def test_build_portfolio_valuation(env):
    execute_trade("AAPL", "buy", 10, 100.0)
    env.update("AAPL", 110.0)  # price rises
    p = build_portfolio()
    assert p["cash_balance"] == pytest.approx(9000.0)
    assert p["positions_value"] == pytest.approx(1100.0)
    assert p["total_value"] == pytest.approx(10100.0)
    assert p["total_unrealized_pl"] == pytest.approx(100.0)
    row = p["positions"][0]
    assert row["ticker"] == "AAPL"
    assert row["unrealized_pl_pct"] == pytest.approx(10.0)


def test_trade_records_snapshot(env):
    before = len(repo.get_snapshots())
    execute_trade("AAPL", "buy", 1, 100.0)
    assert len(repo.get_snapshots()) == before + 1
