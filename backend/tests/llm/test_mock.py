"""Mock-mode pattern matching."""

from __future__ import annotations

from app.llm.mock import mock_response


def test_buy_pattern_extracted() -> None:
    r = mock_response("please buy 10 AAPL for me")
    assert len(r.trades) == 1
    assert r.trades[0].side == "buy"
    assert r.trades[0].quantity == 10
    assert r.trades[0].ticker == "AAPL"


def test_sell_pattern_extracted() -> None:
    r = mock_response("sell 0.5 NVDA")
    assert len(r.trades) == 1
    assert r.trades[0].side == "sell"
    assert r.trades[0].quantity == 0.5


def test_multiple_trades_in_one_message() -> None:
    r = mock_response("buy 5 AAPL and sell 2 GOOGL")
    sides = sorted([t.side for t in r.trades])
    assert sides == ["buy", "sell"]


def test_watchlist_add() -> None:
    r = mock_response("add PYPL to my watchlist")
    assert len(r.watchlist_changes) == 1
    assert r.watchlist_changes[0].ticker == "PYPL"
    assert r.watchlist_changes[0].action == "add"


def test_watchlist_remove() -> None:
    r = mock_response("remove TSLA from the watchlist")
    assert len(r.watchlist_changes) == 1
    assert r.watchlist_changes[0].action == "remove"


def test_watchlist_word_required() -> None:
    """'add PYPL' alone (no 'watchlist') should not trigger a watchlist change."""
    r = mock_response("add PYPL")
    assert r.watchlist_changes == []


def test_no_match_returns_generic_message() -> None:
    r = mock_response("hello there")
    assert r.trades == []
    assert r.watchlist_changes == []
    assert "mock mode" in r.message.lower()
