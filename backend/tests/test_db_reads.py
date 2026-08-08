import pytest

from db import connection as connection_module
from db.reads import load_active_tickers


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_module, "DB_PATH", tmp_path / "finally.db")
    return tmp_path / "finally.db"


def test_load_active_tickers_seeds_default_watchlist(temp_db):
    tickers = load_active_tickers()
    assert set(tickers) == set(connection_module.DEFAULT_WATCHLIST)


def test_load_active_tickers_includes_open_positions_outside_watchlist(temp_db):
    conn = connection_module.get_connection()
    conn.execute(
        "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) "
        "VALUES ('p1', 'default', 'PYPL', 5, 60.0, '2026-08-08T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    tickers = load_active_tickers()
    assert "PYPL" in tickers


def test_load_active_tickers_excludes_closed_positions(temp_db):
    conn = connection_module.get_connection()
    conn.execute(
        "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) "
        "VALUES ('p1', 'default', 'PYPL', 0, 60.0, '2026-08-08T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    tickers = load_active_tickers()
    assert "PYPL" not in tickers
