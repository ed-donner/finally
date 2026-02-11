"""Tests for database initialization and seeding."""

import tempfile
from pathlib import Path

from app.db.database import init_db


class TestDatabaseInit:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.tmp.name)
        self.tmp.close()
        self.db_path.unlink()  # Start fresh

    def test_creates_tables(self):
        conn = init_db(self.db_path)
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        assert "users_profile" in tables
        assert "watchlist" in tables
        assert "positions" in tables
        assert "trades" in tables
        assert "portfolio_snapshots" in tables
        assert "chat_messages" in tables

    def test_seeds_default_user(self):
        conn = init_db(self.db_path)
        user = conn.execute("SELECT * FROM users_profile WHERE id='default'").fetchone()
        assert user is not None
        assert user["cash_balance"] == 10000.0

    def test_seeds_default_watchlist(self):
        conn = init_db(self.db_path)
        tickers = [
            r["ticker"]
            for r in conn.execute("SELECT ticker FROM watchlist ORDER BY ticker").fetchall()
        ]
        assert len(tickers) == 10
        assert "AAPL" in tickers
        assert "GOOGL" in tickers

    def test_idempotent_init(self):
        conn1 = init_db(self.db_path)
        conn1.close()
        conn2 = init_db(self.db_path)
        tickers = conn2.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
        assert tickers == 10  # Not doubled

    def test_wal_mode(self):
        conn = init_db(self.db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
