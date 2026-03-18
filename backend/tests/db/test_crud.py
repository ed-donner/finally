"""Tests for all CRUD operations."""

import pytest

from app.db.crud import (
    add_watchlist_ticker,
    delete_position,
    get_position_by_ticker,
    get_positions,
    get_recent_chat_messages,
    get_user_profile,
    insert_chat_message,
    insert_portfolio_snapshot,
    insert_trade,
    list_portfolio_snapshots,
    list_trades,
    list_watchlist,
    remove_watchlist_ticker,
    update_cash_balance,
    upsert_position,
)


# ---------------------------------------------------------------------------
# users_profile
# ---------------------------------------------------------------------------

class TestUserProfile:
    async def test_get_default_user(self):
        profile = await get_user_profile()
        assert profile is not None
        assert profile["id"] == "default"
        assert profile["cash_balance"] == 10000.0

    async def test_get_nonexistent_user(self):
        profile = await get_user_profile("nobody")
        assert profile is None

    async def test_update_cash_balance(self):
        result = await update_cash_balance(7500.50)
        assert result["cash_balance"] == 7500.50
        profile = await get_user_profile()
        assert profile["cash_balance"] == 7500.50


# ---------------------------------------------------------------------------
# watchlist
# ---------------------------------------------------------------------------

class TestWatchlist:
    async def test_list_default_watchlist(self):
        items = await list_watchlist()
        assert len(items) == 10
        tickers = {i["ticker"] for i in items}
        assert "AAPL" in tickers

    async def test_add_ticker(self):
        result = await add_watchlist_ticker("PYPL")
        assert result["ticker"] == "PYPL"
        items = await list_watchlist()
        assert len(items) == 11

    async def test_add_duplicate_ticker_raises(self):
        with pytest.raises(ValueError, match="already in watchlist"):
            await add_watchlist_ticker("AAPL")

    async def test_add_lowercase_normalizes(self):
        result = await add_watchlist_ticker("pypl")
        assert result["ticker"] == "PYPL"

    async def test_remove_ticker(self):
        removed = await remove_watchlist_ticker("AAPL")
        assert removed is True
        items = await list_watchlist()
        tickers = {i["ticker"] for i in items}
        assert "AAPL" not in tickers

    async def test_remove_nonexistent_ticker(self):
        removed = await remove_watchlist_ticker("ZZZZ")
        assert removed is False


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------

class TestPositions:
    async def test_no_positions_initially(self):
        positions = await get_positions()
        assert positions == []

    async def test_upsert_creates_position(self):
        result = await upsert_position("AAPL", 10, 150.0)
        assert result["ticker"] == "AAPL"
        assert result["quantity"] == 10
        assert result["avg_cost"] == 150.0

    async def test_upsert_updates_existing(self):
        await upsert_position("AAPL", 10, 150.0)
        result = await upsert_position("AAPL", 15, 155.0)
        assert result["quantity"] == 15
        assert result["avg_cost"] == 155.0
        positions = await get_positions()
        assert len(positions) == 1

    async def test_upsert_zero_quantity_deletes(self):
        await upsert_position("AAPL", 10, 150.0)
        result = await upsert_position("AAPL", 0, 0)
        assert result["deleted"] is True
        positions = await get_positions()
        assert len(positions) == 0

    async def test_get_position_by_ticker(self):
        await upsert_position("AAPL", 10, 150.0)
        pos = await get_position_by_ticker("AAPL")
        assert pos is not None
        assert pos["quantity"] == 10

    async def test_get_position_by_ticker_not_found(self):
        pos = await get_position_by_ticker("ZZZZ")
        assert pos is None

    async def test_delete_position(self):
        await upsert_position("AAPL", 10, 150.0)
        deleted = await delete_position("AAPL")
        assert deleted is True
        assert await get_position_by_ticker("AAPL") is None

    async def test_delete_nonexistent_position(self):
        deleted = await delete_position("ZZZZ")
        assert deleted is False

    async def test_lowercase_ticker_normalized(self):
        await upsert_position("aapl", 5, 100.0)
        pos = await get_position_by_ticker("AAPL")
        assert pos is not None
        assert pos["ticker"] == "AAPL"


# ---------------------------------------------------------------------------
# trades
# ---------------------------------------------------------------------------

class TestTrades:
    async def test_insert_buy_trade(self):
        trade = await insert_trade("AAPL", "buy", 10, 150.0)
        assert trade["ticker"] == "AAPL"
        assert trade["side"] == "buy"
        assert trade["quantity"] == 10
        assert trade["price"] == 150.0

    async def test_insert_sell_trade(self):
        trade = await insert_trade("AAPL", "sell", 5, 160.0)
        assert trade["side"] == "sell"

    async def test_invalid_side_raises(self):
        with pytest.raises(ValueError, match="Invalid side"):
            await insert_trade("AAPL", "short", 10, 150.0)

    async def test_list_trades_ordered_by_recent(self):
        await insert_trade("AAPL", "buy", 10, 150.0)
        await insert_trade("GOOGL", "buy", 5, 175.0)
        trades = await list_trades()
        assert len(trades) == 2
        # Most recent first
        assert trades[0]["ticker"] == "GOOGL"

    async def test_list_trades_limit(self):
        for i in range(5):
            await insert_trade("AAPL", "buy", 1, 150.0 + i)
        trades = await list_trades(limit=3)
        assert len(trades) == 3


# ---------------------------------------------------------------------------
# portfolio_snapshots
# ---------------------------------------------------------------------------

class TestPortfolioSnapshots:
    async def test_insert_snapshot(self):
        snap = await insert_portfolio_snapshot(10500.0)
        assert snap["total_value"] == 10500.0
        assert snap["user_id"] == "default"

    async def test_list_snapshots(self):
        await insert_portfolio_snapshot(10000.0)
        await insert_portfolio_snapshot(10500.0)
        snaps = await list_portfolio_snapshots()
        assert len(snaps) == 2
        # Most recent first
        assert snaps[0]["total_value"] == 10500.0

    async def test_list_snapshots_limit(self):
        for i in range(5):
            await insert_portfolio_snapshot(10000.0 + i * 100)
        snaps = await list_portfolio_snapshots(limit=3)
        assert len(snaps) == 3


# ---------------------------------------------------------------------------
# chat_messages
# ---------------------------------------------------------------------------

class TestChatMessages:
    async def test_insert_user_message(self):
        msg = await insert_chat_message("user", "Buy 10 AAPL")
        assert msg["role"] == "user"
        assert msg["content"] == "Buy 10 AAPL"
        assert msg["actions"] is None

    async def test_insert_assistant_message_with_actions(self):
        actions = {"trades": [{"ticker": "AAPL", "side": "buy", "quantity": 10}]}
        msg = await insert_chat_message("assistant", "Done!", actions)
        assert msg["actions"] == actions

    async def test_invalid_role_raises(self):
        with pytest.raises(ValueError, match="Invalid role"):
            await insert_chat_message("system", "hello")

    async def test_get_recent_messages_ordered_oldest_first(self):
        await insert_chat_message("user", "msg1")
        await insert_chat_message("assistant", "msg2")
        await insert_chat_message("user", "msg3")
        msgs = await get_recent_chat_messages(n=20)
        assert len(msgs) == 3
        assert msgs[0]["content"] == "msg1"
        assert msgs[2]["content"] == "msg3"

    async def test_get_recent_messages_limit(self):
        for i in range(5):
            await insert_chat_message("user", f"msg{i}")
        msgs = await get_recent_chat_messages(n=3)
        assert len(msgs) == 3
        # Should be the 3 most recent, oldest first
        assert msgs[0]["content"] == "msg2"
        assert msgs[2]["content"] == "msg4"

    async def test_actions_json_roundtrip(self):
        actions = [{"ticker": "AAPL", "action": "add"}]
        await insert_chat_message("assistant", "Added!", actions)
        msgs = await get_recent_chat_messages(n=1)
        assert msgs[0]["actions"] == actions
