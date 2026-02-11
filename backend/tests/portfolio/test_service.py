"""Tests for portfolio service: trade execution, portfolio queries, history."""

from uuid import uuid4

import pytest

from app.portfolio.service import execute_trade, get_portfolio, get_portfolio_history

# --- Buy execution ---


async def test_buy_deducts_cash(db, price_cache):
    """Buy 10 AAPL at $150 -> cash goes from $10000 to $8500."""
    await execute_trade(db, price_cache, "AAPL", "buy", 10)

    row = await db.execute_fetchall("SELECT cash_balance FROM users_profile WHERE id = 'default'")
    assert row[0][0] == pytest.approx(8500.0)


async def test_buy_creates_position(db, price_cache):
    """Buy 10 AAPL -> position exists with qty=10, avg_cost=150."""
    await execute_trade(db, price_cache, "AAPL", "buy", 10)

    row = await db.execute_fetchall(
        "SELECT quantity, avg_cost FROM positions WHERE user_id = 'default' AND ticker = 'AAPL'"
    )
    assert row[0][0] == pytest.approx(10.0)
    assert row[0][1] == pytest.approx(150.0)


async def test_buy_updates_existing_position_weighted_avg(db, price_cache):
    """Buy 10 AAPL at $150, then buy 10 AAPL at $200 -> qty=20, avg_cost=175."""
    await execute_trade(db, price_cache, "AAPL", "buy", 10)

    # Update price to $200 for second buy
    price_cache.update("AAPL", 200.00)
    await execute_trade(db, price_cache, "AAPL", "buy", 10)

    row = await db.execute_fetchall(
        "SELECT quantity, avg_cost FROM positions WHERE user_id = 'default' AND ticker = 'AAPL'"
    )
    assert row[0][0] == pytest.approx(20.0)
    assert row[0][1] == pytest.approx(175.0)


async def test_buy_insufficient_cash(db, price_cache):
    """Buy 100 AAPL at $150 = $15000 > $10000 -> raises ValueError."""
    with pytest.raises(ValueError, match="Insufficient cash"):
        await execute_trade(db, price_cache, "AAPL", "buy", 100)


# --- Sell execution ---


async def test_sell_adds_cash(db, price_cache):
    """Buy 10 AAPL, sell 5 -> cash = 10000 - 1500 + 750 = 9250."""
    await execute_trade(db, price_cache, "AAPL", "buy", 10)
    await execute_trade(db, price_cache, "AAPL", "sell", 5)

    row = await db.execute_fetchall("SELECT cash_balance FROM users_profile WHERE id = 'default'")
    assert row[0][0] == pytest.approx(9250.0)


async def test_sell_reduces_position(db, price_cache):
    """Buy 10, sell 5 -> qty=5, avg_cost unchanged at 150."""
    await execute_trade(db, price_cache, "AAPL", "buy", 10)
    await execute_trade(db, price_cache, "AAPL", "sell", 5)

    row = await db.execute_fetchall(
        "SELECT quantity, avg_cost FROM positions WHERE user_id = 'default' AND ticker = 'AAPL'"
    )
    assert row[0][0] == pytest.approx(5.0)
    assert row[0][1] == pytest.approx(150.0)


async def test_sell_all_removes_position(db, price_cache):
    """Buy 10, sell 10 -> position row deleted."""
    await execute_trade(db, price_cache, "AAPL", "buy", 10)
    await execute_trade(db, price_cache, "AAPL", "sell", 10)

    row = await db.execute_fetchall(
        "SELECT * FROM positions WHERE user_id = 'default' AND ticker = 'AAPL'"
    )
    assert len(row) == 0


async def test_sell_insufficient_shares(db, price_cache):
    """Sell AAPL without owning any -> raises ValueError."""
    with pytest.raises(ValueError, match="Insufficient shares"):
        await execute_trade(db, price_cache, "AAPL", "sell", 5)


async def test_sell_more_than_owned(db, price_cache):
    """Buy 5, sell 10 -> raises ValueError."""
    await execute_trade(db, price_cache, "AAPL", "buy", 5)
    with pytest.raises(ValueError, match="Insufficient shares"):
        await execute_trade(db, price_cache, "AAPL", "sell", 10)


async def test_sell_does_not_change_avg_cost(db, price_cache):
    """Buy at $150, price changes to $200, sell some -> avg_cost still $150."""
    await execute_trade(db, price_cache, "AAPL", "buy", 10)

    price_cache.update("AAPL", 200.00)
    await execute_trade(db, price_cache, "AAPL", "sell", 5)

    row = await db.execute_fetchall(
        "SELECT avg_cost FROM positions WHERE user_id = 'default' AND ticker = 'AAPL'"
    )
    assert row[0][0] == pytest.approx(150.0)


# --- Trade recording ---


async def test_trade_recorded_in_history(db, price_cache):
    """Execute a buy -> trades table has 1 row with correct fields."""
    await execute_trade(db, price_cache, "AAPL", "buy", 10)

    rows = await db.execute_fetchall("SELECT ticker, side, quantity, price FROM trades")
    assert len(rows) == 1
    assert rows[0][0] == "AAPL"
    assert rows[0][1] == "buy"
    assert rows[0][2] == pytest.approx(10.0)
    assert rows[0][3] == pytest.approx(150.0)


async def test_multiple_trades_all_recorded(db, price_cache):
    """Execute 3 trades -> trades table has 3 rows."""
    await execute_trade(db, price_cache, "AAPL", "buy", 10)
    await execute_trade(db, price_cache, "GOOGL", "buy", 5)
    await execute_trade(db, price_cache, "AAPL", "sell", 3)

    rows = await db.execute_fetchall("SELECT * FROM trades")
    assert len(rows) == 3


# --- Validation ---


async def test_trade_no_price_available(db, price_cache):
    """Trade a ticker not in price_cache -> raises ValueError."""
    with pytest.raises(ValueError, match="No price available"):
        await execute_trade(db, price_cache, "UNKNOWN", "buy", 1)


async def test_buy_rolls_back_on_failure(db, price_cache):
    """Attempt a buy that fails validation -> cash unchanged."""
    with pytest.raises(ValueError, match="Insufficient cash"):
        await execute_trade(db, price_cache, "AAPL", "buy", 100)

    row = await db.execute_fetchall("SELECT cash_balance FROM users_profile WHERE id = 'default'")
    assert row[0][0] == pytest.approx(10000.0)


# --- Portfolio query ---


async def test_get_portfolio_empty(db, price_cache):
    """No positions -> returns cash=10000, positions=[], total_value=10000."""
    result = await get_portfolio(db, price_cache)

    assert result["cash_balance"] == pytest.approx(10000.0)
    assert result["positions"] == []
    assert result["total_value"] == pytest.approx(10000.0)


async def test_get_portfolio_with_positions(db, price_cache):
    """Buy AAPL, query portfolio -> position shows current_price, correct unrealized_pnl."""
    await execute_trade(db, price_cache, "AAPL", "buy", 10)

    # Price goes up to $160
    price_cache.update("AAPL", 160.00)
    result = await get_portfolio(db, price_cache)

    assert len(result["positions"]) == 1
    pos = result["positions"][0]
    assert pos["ticker"] == "AAPL"
    assert pos["quantity"] == pytest.approx(10.0)
    assert pos["avg_cost"] == pytest.approx(150.0)
    assert pos["current_price"] == pytest.approx(160.0)
    assert pos["market_value"] == pytest.approx(1600.0)
    assert pos["unrealized_pnl"] == pytest.approx(100.0)


async def test_get_portfolio_price_fallback(db, price_cache):
    """Buy AAPL, remove from price_cache -> current_price falls back to avg_cost."""
    await execute_trade(db, price_cache, "AAPL", "buy", 10)
    price_cache.remove("AAPL")

    result = await get_portfolio(db, price_cache)
    pos = result["positions"][0]
    assert pos["current_price"] == pytest.approx(150.0)
    assert pos["unrealized_pnl"] == pytest.approx(0.0)


async def test_get_portfolio_total_value(db, price_cache):
    """Buy AAPL and GOOGL -> total_value = cash + AAPL market_value + GOOGL market_value."""
    await execute_trade(db, price_cache, "AAPL", "buy", 10)   # cost 1500
    await execute_trade(db, price_cache, "GOOGL", "buy", 10)  # cost 1750

    result = await get_portfolio(db, price_cache)

    expected_cash = 10000 - 1500 - 1750  # 6750
    expected_aapl_mv = 150.0 * 10  # 1500
    expected_googl_mv = 175.0 * 10  # 1750
    expected_total = expected_cash + expected_aapl_mv + expected_googl_mv  # 10000

    assert result["cash_balance"] == pytest.approx(expected_cash)
    assert result["total_value"] == pytest.approx(expected_total)


# --- Portfolio history ---


async def test_get_portfolio_history_empty(db):
    """No snapshots -> returns empty list."""
    result = await get_portfolio_history(db)
    assert result["snapshots"] == []


async def test_get_portfolio_history_ordered(db):
    """Insert 3 snapshots manually -> returned in chronological order."""
    for i, ts in enumerate(["2024-01-01T00:00:00", "2024-01-01T01:00:00", "2024-01-01T02:00:00"]):
        await db.execute(
            "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) VALUES (?, ?, ?, ?)",
            (str(uuid4()), "default", 10000 + i * 100, ts),
        )
    await db.commit()

    result = await get_portfolio_history(db)
    assert len(result["snapshots"]) == 3
    assert result["snapshots"][0]["total_value"] == pytest.approx(10000.0)
    assert result["snapshots"][1]["total_value"] == pytest.approx(10100.0)
    assert result["snapshots"][2]["total_value"] == pytest.approx(10200.0)
    assert result["snapshots"][0]["recorded_at"] < result["snapshots"][2]["recorded_at"]
