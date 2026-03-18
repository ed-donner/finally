"""Tests for LLM Pydantic models and structured output parsing."""

import json

import pytest
from pydantic import ValidationError

from app.llm.models import LLMResponse, TradeAction, WatchlistChange


class TestTradeAction:
    def test_valid_buy(self):
        t = TradeAction(ticker="AAPL", side="buy", quantity=10)
        assert t.ticker == "AAPL"
        assert t.side == "buy"
        assert t.quantity == 10

    def test_valid_sell(self):
        t = TradeAction(ticker="TSLA", side="sell", quantity=5.5)
        assert t.side == "sell"
        assert t.quantity == 5.5

    def test_invalid_side(self):
        with pytest.raises(ValidationError):
            TradeAction(ticker="AAPL", side="hold", quantity=10)

    def test_missing_fields(self):
        with pytest.raises(ValidationError):
            TradeAction(ticker="AAPL")


class TestWatchlistChange:
    def test_valid_add(self):
        w = WatchlistChange(ticker="PYPL", action="add")
        assert w.ticker == "PYPL"
        assert w.action == "add"

    def test_valid_remove(self):
        w = WatchlistChange(ticker="AAPL", action="remove")
        assert w.action == "remove"

    def test_invalid_action(self):
        with pytest.raises(ValidationError):
            WatchlistChange(ticker="AAPL", action="update")


class TestLLMResponse:
    def test_message_only(self):
        r = LLMResponse(message="Hello!")
        assert r.message == "Hello!"
        assert r.trades == []
        assert r.watchlist_changes == []

    def test_with_trades(self):
        r = LLMResponse(
            message="Buying AAPL",
            trades=[TradeAction(ticker="AAPL", side="buy", quantity=10)],
        )
        assert len(r.trades) == 1
        assert r.trades[0].ticker == "AAPL"

    def test_with_watchlist_changes(self):
        r = LLMResponse(
            message="Added PYPL",
            watchlist_changes=[WatchlistChange(ticker="PYPL", action="add")],
        )
        assert len(r.watchlist_changes) == 1

    def test_full_response(self):
        r = LLMResponse(
            message="Done!",
            trades=[TradeAction(ticker="AAPL", side="buy", quantity=5)],
            watchlist_changes=[WatchlistChange(ticker="PYPL", action="add")],
        )
        assert r.message == "Done!"
        assert len(r.trades) == 1
        assert len(r.watchlist_changes) == 1

    def test_parse_from_json(self):
        """Simulate parsing a JSON string from the LLM."""
        raw = json.dumps({
            "message": "Executed trade",
            "trades": [{"ticker": "GOOGL", "side": "sell", "quantity": 3}],
            "watchlist_changes": [],
        })
        r = LLMResponse.model_validate_json(raw)
        assert r.message == "Executed trade"
        assert r.trades[0].ticker == "GOOGL"

    def test_parse_minimal_json(self):
        """LLM might return only the message field."""
        raw = json.dumps({"message": "Just chatting"})
        r = LLMResponse.model_validate_json(raw)
        assert r.trades == []
        assert r.watchlist_changes == []

    def test_parse_invalid_json_raises(self):
        with pytest.raises(ValidationError):
            LLMResponse.model_validate_json('{"trades": []}')  # missing message

    def test_multiple_trades(self):
        r = LLMResponse(
            message="Rebalancing",
            trades=[
                TradeAction(ticker="AAPL", side="sell", quantity=5),
                TradeAction(ticker="GOOGL", side="buy", quantity=10),
            ],
        )
        assert len(r.trades) == 2
