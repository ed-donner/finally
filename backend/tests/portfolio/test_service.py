"""Trade execution and portfolio valuation tests."""

from __future__ import annotations

import pytest

from app import db
from app.market import PriceCache
from app.portfolio import service


def test_buy_with_no_existing_position(temp_db: str, cache_with_prices: PriceCache) -> None:
    result = service.execute_trade(cache_with_prices, "AAPL", "buy", 10)
    assert result.cash_balance_after == 10_000.0 - 10 * 100.0
    pos = db.positions.get_position("AAPL")
    assert pos is not None
    assert pos.quantity == 10
    assert pos.avg_cost == 100.0


def test_buy_averages_cost_correctly(temp_db: str, cache_with_prices: PriceCache) -> None:
    service.execute_trade(cache_with_prices, "AAPL", "buy", 10)  # @ 100
    cache_with_prices.update("AAPL", 120.0)
    service.execute_trade(cache_with_prices, "AAPL", "buy", 5)  # @ 120
    pos = db.positions.get_position("AAPL")
    assert pos is not None
    assert pos.quantity == 15
    # avg = (10 * 100 + 5 * 120) / 15 = 1600 / 15 = 106.6666...
    assert pos.avg_cost == pytest.approx(1600 / 15)


def test_buy_insufficient_cash_raises(temp_db: str, cache_with_prices: PriceCache) -> None:
    with pytest.raises(service.TradeError, match="Insufficient cash"):
        service.execute_trade(cache_with_prices, "AAPL", "buy", 10_000)


def test_buy_unknown_ticker_raises(temp_db: str, cache_with_prices: PriceCache) -> None:
    with pytest.raises(service.TradeError, match="No live price"):
        service.execute_trade(cache_with_prices, "ZZZZ", "buy", 1)


def test_sell_partial_keeps_avg_cost(temp_db: str, cache_with_prices: PriceCache) -> None:
    service.execute_trade(cache_with_prices, "AAPL", "buy", 10)  # @ 100
    cache_with_prices.update("AAPL", 150.0)
    result = service.execute_trade(cache_with_prices, "AAPL", "sell", 4)
    pos = db.positions.get_position("AAPL")
    assert pos is not None
    assert pos.quantity == 6
    assert pos.avg_cost == 100.0  # avg cost unchanged on sell
    assert result.cash_balance_after == pytest.approx(10_000.0 - 1000.0 + 4 * 150.0)


def test_sell_full_deletes_position(temp_db: str, cache_with_prices: PriceCache) -> None:
    service.execute_trade(cache_with_prices, "AAPL", "buy", 10)
    service.execute_trade(cache_with_prices, "AAPL", "sell", 10)
    assert db.positions.get_position("AAPL") is None


def test_sell_more_than_held_raises(temp_db: str, cache_with_prices: PriceCache) -> None:
    service.execute_trade(cache_with_prices, "AAPL", "buy", 5)
    with pytest.raises(service.TradeError, match="Insufficient shares"):
        service.execute_trade(cache_with_prices, "AAPL", "sell", 10)


def test_sell_with_no_position_raises(temp_db: str, cache_with_prices: PriceCache) -> None:
    with pytest.raises(service.TradeError, match="Insufficient shares"):
        service.execute_trade(cache_with_prices, "AAPL", "sell", 1)


def test_invalid_side_raises(temp_db: str, cache_with_prices: PriceCache) -> None:
    with pytest.raises(service.TradeError):
        service.execute_trade(cache_with_prices, "AAPL", "hodl", 1)  # type: ignore[arg-type]


def test_zero_quantity_raises(temp_db: str, cache_with_prices: PriceCache) -> None:
    with pytest.raises(service.TradeError):
        service.execute_trade(cache_with_prices, "AAPL", "buy", 0)


def test_invalid_ticker_rejected(temp_db: str, cache_with_prices: PriceCache) -> None:
    with pytest.raises(service.TradeError, match="ticker must be 1-10 alphanumeric"):
        service.execute_trade(cache_with_prices, "AAPL!", "buy", 1)


def test_quantity_precision_limited_to_four_decimals(
    temp_db: str, cache_with_prices: PriceCache
) -> None:
    with pytest.raises(service.TradeError, match="at most 4 decimal places"):
        service.execute_trade(cache_with_prices, "AAPL", "buy", 0.12345)


def test_fractional_shares_supported(temp_db: str, cache_with_prices: PriceCache) -> None:
    service.execute_trade(cache_with_prices, "AAPL", "buy", 0.5)
    pos = db.positions.get_position("AAPL")
    assert pos is not None
    assert pos.quantity == 0.5


def test_trade_appended_to_log(temp_db: str, cache_with_prices: PriceCache) -> None:
    service.execute_trade(cache_with_prices, "AAPL", "buy", 5)
    service.execute_trade(cache_with_prices, "AAPL", "sell", 2)
    trades = db.trades.list_recent()
    assert len(trades) == 2
    assert trades[0].side == "sell"
    assert trades[1].side == "buy"


def test_trade_records_snapshot_immediately(temp_db: str, cache_with_prices: PriceCache) -> None:
    before = db.snapshots.list_history()
    service.execute_trade(cache_with_prices, "AAPL", "buy", 1)
    after = db.snapshots.list_history()
    assert len(after) == len(before) + 1
    assert after[-1].total_value == pytest.approx(10_000.0)


def test_portfolio_view_with_pnl(temp_db: str, cache_with_prices: PriceCache) -> None:
    service.execute_trade(cache_with_prices, "AAPL", "buy", 10)  # @ 100
    cache_with_prices.update("AAPL", 110.0)  # +10%
    view = service.get_portfolio_view(cache_with_prices)
    assert view.cash_balance == 9_000.0
    assert len(view.positions) == 1
    p = view.positions[0]
    assert p.market_value == 1100.0
    assert p.unrealized_pnl == 100.0
    assert p.unrealized_pnl_percent == pytest.approx(10.0)
    assert view.total_value == 10_100.0


def test_total_value_falls_back_to_cost_when_no_price(temp_db: str) -> None:
    cache = PriceCache()
    cache.update("AAPL", 100.0)
    service.execute_trade(cache, "AAPL", "buy", 10)
    cache.remove("AAPL")  # price disappears
    # Should not crash and should not zero-out the position
    total = service.total_value(cache)
    assert total == pytest.approx(10_000.0)  # cash 9000 + 10 shares @ avg 100


def test_stale_price_rejected(temp_db: str) -> None:
    cache = PriceCache()
    cache.update(
        "AAPL",
        100.0,
        timestamp=1.0,
    )
    with pytest.raises(service.TradeError, match="stale"):
        service.execute_trade(cache, "AAPL", "buy", 1)
