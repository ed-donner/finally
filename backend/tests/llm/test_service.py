"""Tests for the LLM service (mock mode) and action execution."""


import pytest

from app.db import (
    get_cash_balance,
    get_position,
    get_watchlist,
    init_db,
    set_db_path,
    upsert_position,
)
from app.llm.models import LlmResponse, TradeAction, WatchlistChange
from app.llm.service import _build_context, _execute_actions, chat_with_llm
from app.market import PriceCache


@pytest.fixture
async def test_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    set_db_path(db_path)
    await init_db()
    yield db_path
    set_db_path(str(tmp_path / "unused.db"))


@pytest.fixture
def price_cache():
    cache = PriceCache()
    cache.update("AAPL", 190.50)
    cache.update("GOOGL", 175.25)
    cache.update("MSFT", 420.00)
    cache.update("AMZN", 185.00)
    cache.update("TSLA", 250.00)
    cache.update("NVDA", 880.00)
    cache.update("META", 500.00)
    cache.update("JPM", 195.00)
    cache.update("V", 280.00)
    cache.update("NFLX", 620.00)
    return cache


class TestBuildContext:
    async def test_builds_context_with_defaults(self, test_db, price_cache):
        ctx = await _build_context(price_cache)
        assert ctx["cash"] == 10000.0
        assert ctx["positions"] == []
        assert len(ctx["watchlist"]) == 10
        assert ctx["total_value"] == 10000.0

    async def test_context_with_position(self, test_db, price_cache):
        await upsert_position("AAPL", 10, 180.0)
        ctx = await _build_context(price_cache)
        assert len(ctx["positions"]) == 1
        pos = ctx["positions"][0]
        assert pos["ticker"] == "AAPL"
        assert pos["quantity"] == 10
        assert pos["current_price"] == 190.50
        assert pos["unrealized_pnl"] == 105.0  # (190.50 - 180) * 10


class TestExecuteActions:
    async def test_execute_buy(self, test_db, price_cache):
        resp = LlmResponse(
            message="Buying",
            trades=[TradeAction(ticker="AAPL", side="buy", quantity=5)],
        )
        results = await _execute_actions(resp, price_cache)
        assert results["trades"][0]["status"] == "executed"
        assert results["trades"][0]["price"] == 190.50

        cash = await get_cash_balance()
        assert cash == pytest.approx(10000 - 190.50 * 5)

        pos = await get_position("AAPL")
        assert pos["quantity"] == 5

    async def test_execute_sell_insufficient_shares(self, test_db, price_cache):
        resp = LlmResponse(
            message="Selling",
            trades=[TradeAction(ticker="AAPL", side="sell", quantity=5)],
        )
        results = await _execute_actions(resp, price_cache)
        assert "error" in results["trades"][0]
        assert "Insufficient shares" in results["trades"][0]["error"]

    async def test_execute_buy_insufficient_cash(self, test_db, price_cache):
        resp = LlmResponse(
            message="Buying",
            trades=[TradeAction(ticker="NVDA", side="buy", quantity=100)],
        )
        results = await _execute_actions(resp, price_cache)
        assert "error" in results["trades"][0]
        assert "Insufficient cash" in results["trades"][0]["error"]

    async def test_execute_watchlist_add(self, test_db, price_cache):
        resp = LlmResponse(
            message="Adding",
            watchlist_changes=[WatchlistChange(ticker="PYPL", action="add")],
        )
        results = await _execute_actions(resp, price_cache)
        assert results["watchlist_changes"][0]["status"] == "done"

        wl = await get_watchlist()
        tickers = [w["ticker"] for w in wl]
        assert "PYPL" in tickers

    async def test_execute_watchlist_remove(self, test_db, price_cache):
        resp = LlmResponse(
            message="Removing",
            watchlist_changes=[WatchlistChange(ticker="AAPL", action="remove")],
        )
        results = await _execute_actions(resp, price_cache)
        assert results["watchlist_changes"][0]["status"] == "done"

        wl = await get_watchlist()
        tickers = [w["ticker"] for w in wl]
        assert "AAPL" not in tickers

    async def test_no_price_available(self, test_db, price_cache):
        resp = LlmResponse(
            message="Buying",
            trades=[TradeAction(ticker="ZZZZ", side="buy", quantity=1)],
        )
        results = await _execute_actions(resp, price_cache)
        assert "error" in results["trades"][0]
        assert "No price" in results["trades"][0]["error"]


class TestChatWithLlmMock:
    @pytest.fixture(autouse=True)
    def set_mock_mode(self, monkeypatch):
        monkeypatch.setenv("LLM_MOCK", "true")

    async def test_greeting(self, test_db, price_cache):
        result = await chat_with_llm("hello", price_cache)
        assert "FinAlly" in result["message"]
        assert result["trades"] == []

    async def test_buy_executes(self, test_db, price_cache):
        result = await chat_with_llm("buy 5 AAPL", price_cache)
        assert len(result["trades"]) == 1
        assert result["trades"][0]["status"] == "executed"

        pos = await get_position("AAPL")
        assert pos["quantity"] == 5

    async def test_portfolio_analysis(self, test_db, price_cache):
        result = await chat_with_llm("show my portfolio", price_cache)
        assert "10,000.00" in result["message"]
