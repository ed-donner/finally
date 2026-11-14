"""Deterministic mock rules (E2E tests depend on these — TEAM_CONTRACTS §6)."""

import pytest

from app.llm.mock import MOCK_DEFAULT_MESSAGE, is_mock_enabled, mock_response


def test_buy():
    r = mock_response("Buy 5 NVDA please")
    assert r.message.startswith("Mock: buying")
    assert [(t.side, t.quantity, t.ticker) for t in r.trades] == [("buy", 5.0, "NVDA")]
    assert r.watchlist_changes == []


def test_buy_case_insensitive_and_trailing_punctuation():
    r = mock_response("BUY 10 aapl.")
    assert r.message.startswith("Mock: buying")
    assert r.trades[0].ticker == "AAPL" and r.trades[0].quantity == 10


def test_fractional_quantity():
    assert mock_response("buy 2.5 TSLA").trades[0].quantity == 2.5


def test_sell():
    r = mock_response("sell 3 MSFT")
    assert [(t.side, t.quantity, t.ticker) for t in r.trades] == [("sell", 3.0, "MSFT")]
    assert r.message.startswith("Mock: selling")


def test_buy_reply_starts_with_buying_even_if_sell_first():
    r = mock_response("sell 1 MSFT and buy 2 AAPL")
    assert r.message.startswith("Mock: buying")
    assert {t.ticker for t in r.trades} == {"MSFT", "AAPL"}


@pytest.mark.parametrize("text", ["add PYPL", "Watch pypl", "please ADD PYPL to my list"])
def test_watchlist_add(text):
    r = mock_response(text)
    assert [(c.action, c.ticker) for c in r.watchlist_changes] == [("add", "PYPL")]
    assert r.trades == []


def test_watchlist_remove():
    r = mock_response("remove NFLX")
    assert [(c.action, c.ticker) for c in r.watchlist_changes] == [("remove", "NFLX")]


@pytest.mark.parametrize("text", ["hello", "What's in my portfolio?", "buy some apple", "watchlist?"])
def test_default(text):
    r = mock_response(text)
    assert r.message == MOCK_DEFAULT_MESSAGE
    assert r.trades == [] and r.watchlist_changes == []


def test_deterministic():
    assert mock_response("buy 5 NVDA") == mock_response("buy 5 NVDA")


@pytest.mark.parametrize("value,expected", [("true", True), ("TRUE", True), ("1", True),
                                            ("false", False), ("", False)])
def test_is_mock_enabled(monkeypatch, value, expected):
    monkeypatch.setenv("LLM_MOCK", value)
    assert is_mock_enabled() is expected
