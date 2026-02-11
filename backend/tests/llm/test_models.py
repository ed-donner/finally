"""Tests for LLM structured output models."""

from app.llm.models import LlmResponse, TradeAction, WatchlistChange


class TestLlmResponse:
    def test_message_only(self):
        resp = LlmResponse(message="Hello")
        assert resp.message == "Hello"
        assert resp.trades == []
        assert resp.watchlist_changes == []

    def test_with_trades(self):
        resp = LlmResponse(
            message="Buying AAPL",
            trades=[TradeAction(ticker="AAPL", side="buy", quantity=10)],
        )
        assert len(resp.trades) == 1
        assert resp.trades[0].ticker == "AAPL"

    def test_with_watchlist_changes(self):
        resp = LlmResponse(
            message="Adding PYPL",
            watchlist_changes=[WatchlistChange(ticker="PYPL", action="add")],
        )
        assert len(resp.watchlist_changes) == 1

    def test_parse_from_json(self):
        raw = '{"message": "Done", "trades": [{"ticker": "MSFT", "side": "sell", "quantity": 5}], "watchlist_changes": []}'
        resp = LlmResponse.model_validate_json(raw)
        assert resp.message == "Done"
        assert resp.trades[0].ticker == "MSFT"
        assert resp.trades[0].side == "sell"
        assert resp.trades[0].quantity == 5

    def test_parse_minimal_json(self):
        raw = '{"message": "Hi"}'
        resp = LlmResponse.model_validate_json(raw)
        assert resp.message == "Hi"
        assert resp.trades == []
        assert resp.watchlist_changes == []
