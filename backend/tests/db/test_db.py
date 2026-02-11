"""Tests for the database layer."""

import json

import pytest

from app.db import (
    DEFAULT_CASH_BALANCE,
    DEFAULT_TICKERS,
    add_to_watchlist,
    delete_position,
    get_cash_balance,
    get_chat_history,
    get_portfolio_history,
    get_position,
    get_positions,
    get_watchlist,
    init_db,
    insert_chat_message,
    insert_snapshot,
    insert_trade,
    remove_from_watchlist,
    set_db_path,
    update_cash_balance,
    upsert_position,
)


@pytest.fixture(autouse=True)
async def temp_db(tmp_path):
    """Use a fresh temporary database for each test."""
    db_path = str(tmp_path / "test.db")
    set_db_path(db_path)
    await init_db()
    yield db_path
    # Clean up the global path
    set_db_path(db_path)


class TestInitialization:
    async def test_init_creates_default_user(self):
        balance = await get_cash_balance()
        assert balance == DEFAULT_CASH_BALANCE

    async def test_init_seeds_watchlist(self):
        watchlist = await get_watchlist()
        tickers = [entry["ticker"] for entry in watchlist]
        assert sorted(tickers) == sorted(DEFAULT_TICKERS)

    async def test_init_is_idempotent(self):
        await init_db()
        await init_db()
        balance = await get_cash_balance()
        assert balance == DEFAULT_CASH_BALANCE
        watchlist = await get_watchlist()
        assert len(watchlist) == len(DEFAULT_TICKERS)


class TestCashBalance:
    async def test_get_default_balance(self):
        balance = await get_cash_balance()
        assert balance == 10000.0

    async def test_update_balance(self):
        await update_cash_balance(5000.0)
        balance = await get_cash_balance()
        assert balance == 5000.0

    async def test_update_balance_to_zero(self):
        await update_cash_balance(0.0)
        balance = await get_cash_balance()
        assert balance == 0.0


class TestWatchlist:
    async def test_get_default_watchlist(self):
        watchlist = await get_watchlist()
        assert len(watchlist) == 10
        assert all("ticker" in entry for entry in watchlist)

    async def test_add_ticker(self):
        entry = await add_to_watchlist("PYPL")
        assert entry["ticker"] == "PYPL"
        watchlist = await get_watchlist()
        tickers = [e["ticker"] for e in watchlist]
        assert "PYPL" in tickers

    async def test_add_ticker_uppercases(self):
        entry = await add_to_watchlist("pypl")
        assert entry["ticker"] == "PYPL"

    async def test_add_duplicate_raises(self):
        with pytest.raises(Exception):
            await add_to_watchlist("AAPL")

    async def test_remove_ticker(self):
        removed = await remove_from_watchlist("AAPL")
        assert removed is True
        watchlist = await get_watchlist()
        tickers = [e["ticker"] for e in watchlist]
        assert "AAPL" not in tickers

    async def test_remove_nonexistent_returns_false(self):
        removed = await remove_from_watchlist("ZZZZ")
        assert removed is False


class TestPositions:
    async def test_no_positions_initially(self):
        positions = await get_positions()
        assert positions == []

    async def test_upsert_creates_position(self):
        pos = await upsert_position("AAPL", 10, 150.0)
        assert pos["ticker"] == "AAPL"
        assert pos["quantity"] == 10
        assert pos["avg_cost"] == 150.0

    async def test_upsert_updates_existing(self):
        await upsert_position("AAPL", 10, 150.0)
        pos = await upsert_position("AAPL", 20, 155.0)
        assert pos["quantity"] == 20
        assert pos["avg_cost"] == 155.0

        positions = await get_positions()
        assert len(positions) == 1

    async def test_get_position_by_ticker(self):
        await upsert_position("AAPL", 10, 150.0)
        pos = await get_position("AAPL")
        assert pos is not None
        assert pos["ticker"] == "AAPL"

    async def test_get_nonexistent_position(self):
        pos = await get_position("ZZZZ")
        assert pos is None

    async def test_delete_position(self):
        await upsert_position("AAPL", 10, 150.0)
        deleted = await delete_position("AAPL")
        assert deleted is True
        pos = await get_position("AAPL")
        assert pos is None

    async def test_delete_nonexistent_returns_false(self):
        deleted = await delete_position("ZZZZ")
        assert deleted is False


class TestTrades:
    async def test_insert_trade(self):
        trade = await insert_trade("AAPL", "buy", 10, 150.0)
        assert trade["ticker"] == "AAPL"
        assert trade["side"] == "buy"
        assert trade["quantity"] == 10
        assert trade["price"] == 150.0
        assert "id" in trade
        assert "executed_at" in trade

    async def test_trade_ticker_uppercased(self):
        trade = await insert_trade("aapl", "buy", 5, 100.0)
        assert trade["ticker"] == "AAPL"


class TestPortfolioSnapshots:
    async def test_insert_and_get_history(self):
        await insert_snapshot(10000.0)
        await insert_snapshot(10500.0)
        await insert_snapshot(10200.0)

        history = await get_portfolio_history()
        assert len(history) == 3
        assert history[0]["total_value"] == 10000.0
        assert history[2]["total_value"] == 10200.0

    async def test_empty_history(self):
        history = await get_portfolio_history()
        assert history == []


class TestChatMessages:
    async def test_insert_and_get_messages(self):
        await insert_chat_message("user", "Hello")
        await insert_chat_message("assistant", "Hi there!", json.dumps({"trades": []}))

        messages = await get_chat_history()
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello"
        assert messages[0]["actions"] is None
        assert messages[1]["role"] == "assistant"
        assert messages[1]["actions"] is not None

    async def test_chat_history_limit(self):
        for i in range(10):
            await insert_chat_message("user", f"Message {i}")

        messages = await get_chat_history(limit=5)
        assert len(messages) == 5
        # Should be the most recent 5, ordered oldest-first
        assert messages[0]["content"] == "Message 5"
        assert messages[4]["content"] == "Message 9"

    async def test_empty_chat_history(self):
        messages = await get_chat_history()
        assert messages == []
