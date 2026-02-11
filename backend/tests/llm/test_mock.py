"""Tests for the mock LLM module."""

from app.llm.mock import mock_chat


class TestMockChat:
    def test_greeting(self):
        result = mock_chat("hello", {})
        assert "FinAlly" in result.message
        assert result.trades == []
        assert result.watchlist_changes == []

    def test_buy_request(self):
        result = mock_chat("buy 10 AAPL", {})
        assert len(result.trades) == 1
        assert result.trades[0].ticker == "AAPL"
        assert result.trades[0].side == "buy"
        assert result.trades[0].quantity == 10

    def test_buy_shares_of(self):
        result = mock_chat("buy 5 shares of MSFT", {})
        assert len(result.trades) == 1
        assert result.trades[0].ticker == "MSFT"
        assert result.trades[0].quantity == 5

    def test_sell_request(self):
        result = mock_chat("sell 3 GOOGL", {})
        assert len(result.trades) == 1
        assert result.trades[0].ticker == "GOOGL"
        assert result.trades[0].side == "sell"
        assert result.trades[0].quantity == 3

    def test_portfolio_analysis_no_positions(self):
        context = {"cash": 10000.0, "positions": [], "total_value": 10000.0}
        result = mock_chat("show my portfolio", context)
        assert "10,000.00" in result.message
        assert result.trades == []

    def test_portfolio_analysis_with_positions(self):
        context = {
            "cash": 5000.0,
            "positions": [{"ticker": "AAPL"}, {"ticker": "MSFT"}],
            "total_value": 8000.0,
        }
        result = mock_chat("analyze my portfolio", context)
        assert "8,000.00" in result.message
        assert "AAPL" in result.message

    def test_watch_ticker(self):
        result = mock_chat("watch PYPL", {})
        assert len(result.watchlist_changes) == 1
        assert result.watchlist_changes[0].ticker == "PYPL"
        assert result.watchlist_changes[0].action == "add"

    def test_add_to_watchlist(self):
        result = mock_chat("add TSLA to my watchlist", {})
        assert len(result.watchlist_changes) == 1
        assert result.watchlist_changes[0].ticker == "TSLA"
