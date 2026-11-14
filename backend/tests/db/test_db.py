"""Tests for app.db: schema, lazy init, seeding and repository functions."""

import sqlite3
import threading

import pytest

import app.db as db
from app.db import connection


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("FINALLY_DB_PATH", str(path))
    db.init_db(path)
    return path


# --- paths & init --------------------------------------------------------------------------


def test_get_db_path_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FINALLY_DB_PATH", str(tmp_path / "x.db"))
    assert db.get_db_path() == tmp_path / "x.db"


def test_get_db_path_default(monkeypatch):
    monkeypatch.delenv("FINALLY_DB_PATH", raising=False)
    path = db.get_db_path()
    assert path.name == "finally.db"
    assert path.parent.name == "db"
    assert (path.parent.parent / "backend" / "app" / "db" / "schema.sql").exists()


def test_init_creates_parent_dir_and_tables(tmp_path):
    path = tmp_path / "nested" / "dir" / "f.db"
    db.init_db(path)
    assert path.exists()
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {
        "users_profile",
        "watchlist",
        "positions",
        "trades",
        "portfolio_snapshots",
        "chat_messages",
    } <= tables


def test_user_id_columns_present(db_path):
    conn = sqlite3.connect(db_path)
    for table in ("watchlist", "positions", "trades", "portfolio_snapshots", "chat_messages"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert "user_id" in cols, table
    conn.close()


def test_wal_mode_and_row_factory(db_path):
    conn = db.get_connection()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_seed_data(db_path):
    assert db.get_cash_balance() == 10000.0
    assert db.get_watchlist() == [
        "AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX",
    ]
    assert db.get_positions() == []
    assert db.get_trades() == []
    assert db.get_snapshots() == []
    assert db.get_chat_messages() == []


def test_init_is_idempotent(db_path):
    db.set_cash_balance(5.0)
    db.remove_watchlist_ticker("AAPL")
    db.init_db(db_path)
    db.init_db(db_path)
    assert db.get_cash_balance() == 5.0
    wl = db.get_watchlist()
    assert "AAPL" not in wl and len(wl) == 9


def test_empty_watchlist_not_reseeded(db_path):
    for t in db.get_watchlist():
        db.remove_watchlist_ticker(t)
    db.init_db(db_path)
    assert db.get_watchlist() == []


def test_lazy_init_without_explicit_call(tmp_path, monkeypatch):
    path = tmp_path / "lazy" / "f.db"
    monkeypatch.setenv("FINALLY_DB_PATH", str(path))
    assert len(db.get_watchlist()) == 10
    assert path.exists()


def test_lazy_reinit_after_file_deleted(db_path):
    db.set_cash_balance(1.0)
    for suffix in ("", "-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix)
        if p.exists():
            p.unlink()
    assert db.get_cash_balance() == 10000.0


# --- users_profile -------------------------------------------------------------------------


def test_set_and_get_cash(db_path):
    db.set_cash_balance(1234.56)
    assert db.get_cash_balance() == pytest.approx(1234.56)


def test_cash_other_user(db_path):
    assert db.get_cash_balance(user_id="alice") == 0.0
    db.set_cash_balance(50.0, user_id="alice")
    assert db.get_cash_balance(user_id="alice") == 50.0
    assert db.get_cash_balance() == 10000.0


# --- watchlist -----------------------------------------------------------------------------


def test_add_watchlist_ticker_normalizes_and_appends(db_path):
    assert db.add_watchlist_ticker(" pypl ") is True
    assert db.get_watchlist()[-1] == "PYPL"


def test_add_duplicate_returns_false(db_path):
    assert db.add_watchlist_ticker("AAPL") is False
    assert db.add_watchlist_ticker("aapl") is False
    assert db.get_watchlist().count("AAPL") == 1


def test_remove_watchlist_ticker(db_path):
    assert db.remove_watchlist_ticker("msft") is True
    assert "MSFT" not in db.get_watchlist()
    assert db.remove_watchlist_ticker("MSFT") is False


def test_watchlist_per_user(db_path):
    assert db.get_watchlist(user_id="bob") == []
    assert db.add_watchlist_ticker("AAPL", user_id="bob") is True
    assert db.get_watchlist(user_id="bob") == ["AAPL"]


def test_watchlist_order_by_added(db_path):
    for t in ("ZZZ", "AAA", "MMM"):
        db.add_watchlist_ticker(t)
    assert db.get_watchlist()[-3:] == ["ZZZ", "AAA", "MMM"]


def test_watchlist_unique_constraint_raw(db_path):
    conn = db.get_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES ('x','default','AAPL','t')"
            )
    finally:
        conn.close()


# --- positions -----------------------------------------------------------------------------


def test_position_crud(db_path):
    assert db.get_position("AAPL") is None
    db.upsert_position("aapl", 10, 190.0)
    pos = db.get_position("AAPL")
    assert pos["ticker"] == "AAPL"
    assert pos["quantity"] == 10.0
    assert pos["avg_cost"] == 190.0
    assert pos["updated_at"]
    assert set(pos) == {"ticker", "quantity", "avg_cost", "updated_at"}

    db.upsert_position("AAPL", 15.5, 192.0)
    pos = db.get_position("AAPL")
    assert pos["quantity"] == 15.5 and pos["avg_cost"] == 192.0
    assert len(db.get_positions()) == 1

    db.delete_position("aapl")
    assert db.get_position("AAPL") is None
    db.delete_position("AAPL")  # no error when absent


def test_get_positions_multiple(db_path):
    db.upsert_position("MSFT", 1, 400)
    db.upsert_position("AAPL", 2, 190)
    db.upsert_position("AAPL", 3, 200, user_id="other")
    tickers = [p["ticker"] for p in db.get_positions()]
    assert sorted(tickers) == ["AAPL", "MSFT"]
    assert db.get_positions(user_id="other")[0]["quantity"] == 3


def test_positions_unique_constraint_raw(db_path):
    db.upsert_position("AAPL", 1, 1)
    conn = db.get_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) "
                "VALUES ('x','default','AAPL',1,1,'t')"
            )
    finally:
        conn.close()


# --- trades --------------------------------------------------------------------------------


def test_record_trade_returns_row(db_path):
    t = db.record_trade("nvda", "BUY", 5, 880.12)
    assert set(t) == {"id", "ticker", "side", "quantity", "price", "executed_at"}
    assert t["ticker"] == "NVDA" and t["side"] == "buy"
    assert t["quantity"] == 5.0 and t["price"] == 880.12
    assert db.get_trades() == [t]


def test_record_trade_invalid_side(db_path):
    with pytest.raises(ValueError):
        db.record_trade("AAPL", "short", 1, 1)


def test_trades_newest_first_and_limit(db_path):
    ids = [db.record_trade("AAPL", "buy", i + 1, 100)["id"] for i in range(5)]
    got = db.get_trades()
    assert [t["id"] for t in got] == list(reversed(ids))
    assert [t["id"] for t in db.get_trades(limit=2)] == [ids[4], ids[3]]
    assert db.get_trades(user_id="other") == []


# --- snapshots -----------------------------------------------------------------------------


def test_snapshots_oldest_first_and_limit(db_path):
    values = [10000.0, 10010.0, 9990.0, 10050.0]
    for v in values:
        snap = db.record_snapshot(v)
        assert snap["total_value"] == v and snap["recorded_at"]
    got = db.get_snapshots()
    assert [s["total_value"] for s in got] == values
    assert set(got[0]) == {"total_value", "recorded_at"}
    # limit keeps the most recent N, still oldest first
    assert [s["total_value"] for s in db.get_snapshots(limit=2)] == [9990.0, 10050.0]


# --- chat ----------------------------------------------------------------------------------


def test_chat_messages_round_trip(db_path):
    u = db.add_chat_message("user", "Buy 5 NVDA")
    actions = {
        "trades": [{"ticker": "NVDA", "side": "buy", "quantity": 5, "status": "executed",
                    "price": 880.12, "error": None}],
        "watchlist_changes": [],
    }
    a = db.add_chat_message("assistant", "Done", actions)
    assert u["actions"] is None
    assert set(a) == {"id", "role", "content", "actions", "created_at"}
    msgs = db.get_chat_messages()
    assert [m["id"] for m in msgs] == [u["id"], a["id"]]
    assert msgs[0]["actions"] is None
    assert msgs[1]["actions"] == actions


def test_chat_actions_list(db_path):
    db.add_chat_message("assistant", "x", [1, {"a": 2}])
    assert db.get_chat_messages()[0]["actions"] == [1, {"a": 2}]


def test_chat_limit_keeps_latest_oldest_first(db_path):
    for i in range(6):
        db.add_chat_message("user", f"m{i}")
    assert [m["content"] for m in db.get_chat_messages(limit=3)] == ["m3", "m4", "m5"]


def test_chat_invalid_role(db_path):
    with pytest.raises(ValueError):
        db.add_chat_message("system", "x")


def test_chat_per_user(db_path):
    db.add_chat_message("user", "hi", user_id="alice")
    assert db.get_chat_messages() == []
    assert len(db.get_chat_messages(user_id="alice")) == 1


# --- transactions --------------------------------------------------------------------------


def test_transaction_commits(db_path):
    with db.transaction() as conn:
        db.set_cash_balance(500.0, conn=conn)
        db.upsert_position("AAPL", 1, 100, conn=conn)
        db.record_trade("AAPL", "buy", 1, 100, conn=conn)
        # visible inside the transaction
        assert db.get_cash_balance(conn=conn) == 500.0
    assert db.get_cash_balance() == 500.0
    assert db.get_position("AAPL") is not None
    assert len(db.get_trades()) == 1


def test_transaction_rolls_back_on_exception(db_path):
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            db.set_cash_balance(1.0, conn=conn)
            db.upsert_position("AAPL", 1, 100, conn=conn)
            db.record_trade("AAPL", "buy", 1, 100, conn=conn)
            db.add_watchlist_ticker("PYPL", conn=conn)
            raise RuntimeError("boom")
    assert db.get_cash_balance() == 10000.0
    assert db.get_position("AAPL") is None
    assert db.get_trades() == []
    assert "PYPL" not in db.get_watchlist()


def test_transaction_rolls_back_on_integrity_error(db_path):
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            db.set_cash_balance(1.0, conn=conn)
            conn.execute(
                "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES ('x','default','AAPL','t')"
            )
    assert db.get_cash_balance() == 10000.0


def test_transaction_uncommitted_invisible_to_other_connection(db_path):
    with db.transaction() as conn:
        db.set_cash_balance(1.0, conn=conn)
        assert db.get_cash_balance() == 10000.0  # separate connection, WAL snapshot
    assert db.get_cash_balance() == 1.0


# --- threading -----------------------------------------------------------------------------


def test_concurrent_writes_from_threads(db_path):
    errors = []

    def worker(n):
        try:
            for i in range(10):
                with db.transaction() as conn:
                    cash = db.get_cash_balance(conn=conn)
                    db.set_cash_balance(cash - 1, conn=conn)
                    db.record_trade("AAPL", "buy", 1, 1, conn=conn)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert db.get_cash_balance() == 10000.0 - 50
    assert len(db.get_trades(limit=1000)) == 50


def test_connection_usable_across_threads(db_path):
    conn = db.get_connection()
    result = []
    try:
        t = threading.Thread(target=lambda: result.append(db.get_cash_balance(conn=conn)))
        t.start()
        t.join()
    finally:
        conn.close()
    assert result == [10000.0]


def test_initialized_cache_tracks_paths(db_path):
    assert str(db_path.resolve()) in connection._initialized
