"""Tests for the repository function surface (app.db.repo)."""

from app.db import repo


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #
def test_get_profile(db):
    profile = repo.get_profile()
    assert profile["id"] == "default"
    assert profile["cash_balance"] == 10000.0
    assert "created_at" in profile


def test_set_cash_balance(db):
    repo.set_cash_balance(8500.5)
    assert repo.get_profile()["cash_balance"] == 8500.5


# --------------------------------------------------------------------------- #
# Watchlist
# --------------------------------------------------------------------------- #
def test_get_watchlist_seeded_order(db):
    assert repo.get_watchlist()[0] == "AAPL"
    assert len(repo.get_watchlist()) == 10


def test_add_watchlist(db):
    assert repo.add_watchlist("PYPL") is True
    assert "PYPL" in repo.get_watchlist()
    assert repo.get_watchlist()[-1] == "PYPL"  # appended at the end


def test_add_watchlist_duplicate_returns_false(db):
    assert repo.add_watchlist("AAPL") is False
    assert repo.get_watchlist().count("AAPL") == 1


def test_remove_watchlist(db):
    assert repo.remove_watchlist("AAPL") is True
    assert "AAPL" not in repo.get_watchlist()


def test_remove_watchlist_absent_returns_false(db):
    assert repo.remove_watchlist("ZZZZ") is False


# --------------------------------------------------------------------------- #
# Positions
# --------------------------------------------------------------------------- #
def test_get_positions_empty_initially(db):
    assert repo.get_positions() == []


def test_upsert_and_get_position(db):
    repo.upsert_position("AAPL", 10, 190.0)
    pos = repo.get_position("AAPL")
    assert pos["ticker"] == "AAPL"
    assert pos["quantity"] == 10
    assert pos["avg_cost"] == 190.0
    assert "updated_at" in pos


def test_upsert_updates_existing(db):
    repo.upsert_position("AAPL", 10, 190.0)
    repo.upsert_position("AAPL", 15, 195.0)
    assert len(repo.get_positions()) == 1
    pos = repo.get_position("AAPL")
    assert pos["quantity"] == 15
    assert pos["avg_cost"] == 195.0


def test_get_position_missing_returns_none(db):
    assert repo.get_position("AAPL") is None


def test_delete_position(db):
    repo.upsert_position("AAPL", 10, 190.0)
    repo.delete_position("AAPL")
    assert repo.get_position("AAPL") is None
    assert repo.get_positions() == []


def test_fractional_shares(db):
    repo.upsert_position("NVDA", 2.5, 800.0)
    assert repo.get_position("NVDA")["quantity"] == 2.5


# --------------------------------------------------------------------------- #
# Trades
# --------------------------------------------------------------------------- #
def test_insert_trade_returns_row(db):
    trade = repo.insert_trade("AAPL", "buy", 5, 191.0)
    assert trade["ticker"] == "AAPL"
    assert trade["side"] == "buy"
    assert trade["quantity"] == 5
    assert trade["price"] == 191.0
    assert "id" in trade and trade["id"]
    assert "executed_at" in trade


def test_get_trades_newest_first(db):
    repo.insert_trade("AAPL", "buy", 5, 191.0)
    repo.insert_trade("GOOGL", "buy", 2, 175.0)
    repo.insert_trade("AAPL", "sell", 1, 195.0)
    trades = repo.get_trades()
    assert len(trades) == 3
    # Most recent first
    assert trades[0]["ticker"] == "AAPL" and trades[0]["side"] == "sell"


def test_get_trades_respects_limit(db):
    for _ in range(5):
        repo.insert_trade("AAPL", "buy", 1, 190.0)
    assert len(repo.get_trades(limit=3)) == 3


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #
def test_insert_and_get_snapshots_ascending(db):
    repo.insert_snapshot(10000.0)
    repo.insert_snapshot(10250.0)
    repo.insert_snapshot(9800.0)
    snaps = repo.get_snapshots()
    assert [s["total_value"] for s in snaps] == [10000.0, 10250.0, 9800.0]
    assert set(snaps[0].keys()) == {"total_value", "recorded_at"}


def test_get_snapshots_limit_keeps_most_recent_ascending(db):
    for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
        repo.insert_snapshot(v)
    snaps = repo.get_snapshots(limit=2)
    # Two most recent, still ascending by time
    assert [s["total_value"] for s in snaps] == [4.0, 5.0]


# --------------------------------------------------------------------------- #
# Chat messages
# --------------------------------------------------------------------------- #
def test_insert_and_get_chat_messages_ascending(db):
    repo.insert_chat_message("user", "buy 5 AAPL", None)
    repo.insert_chat_message(
        "assistant", "Bought 5 AAPL", {"trades": [{"ticker": "AAPL"}]}
    )
    msgs = repo.get_chat_messages()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "buy 5 AAPL"
    assert msgs[0]["actions"] is None
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["actions"] == {"trades": [{"ticker": "AAPL"}]}


def test_get_chat_messages_limit(db):
    for i in range(5):
        repo.insert_chat_message("user", f"msg {i}", None)
    msgs = repo.get_chat_messages(limit=2)
    assert len(msgs) == 2
    # Two most recent, ascending
    assert [m["content"] for m in msgs] == ["msg 3", "msg 4"]


# --------------------------------------------------------------------------- #
# Multi-user isolation
# --------------------------------------------------------------------------- #
def test_user_id_isolation(db):
    repo.upsert_position("AAPL", 10, 190.0, user_id="alice")
    assert repo.get_positions(user_id="alice") != []
    assert repo.get_positions() == []  # default user unaffected
