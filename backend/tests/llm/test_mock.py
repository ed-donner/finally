"""Deterministic mock LLM tests (TEAM_CONTRACT §4 rules)."""

from app.llm.mock import mock_response
from app.llm.schema import ChatResponse

_PF = {"total_value": 10000.0, "total_unrealized_pl": 0.0, "positions": []}


def test_buy_parsed():
    r = mock_response("buy 5 AAPL", _PF)
    assert len(r.trades) == 1
    assert r.trades[0].ticker == "AAPL"
    assert r.trades[0].side == "buy"
    assert r.trades[0].quantity == 5


def test_sell_parsed():
    r = mock_response("please sell 2.5 TSLA now", _PF)
    assert r.trades[0].side == "sell"
    assert r.trades[0].quantity == 2.5
    assert r.trades[0].ticker == "TSLA"


def test_add_remove_watchlist():
    assert mock_response("add PYPL", _PF).watchlist_changes[0].action == "add"
    assert mock_response("remove NFLX", _PF).watchlist_changes[0].action == "remove"


def test_analytical_no_actions():
    r = mock_response("what is my risk exposure?", _PF)
    assert r.trades == []
    assert r.watchlist_changes == []
    assert "mock" in r.message.lower()


def test_multiple_trades():
    r = mock_response("buy 5 AAPL and buy 3 MSFT", _PF)
    assert len(r.trades) == 2


def test_schema_roundtrip():
    r = mock_response("buy 1 AAPL", _PF)
    dumped = r.model_dump_json()
    assert ChatResponse.model_validate_json(dumped).trades[0].ticker == "AAPL"
