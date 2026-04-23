"""Trades repository tests."""

from __future__ import annotations

import time

from app import db


def test_record_and_list(temp_db: str) -> None:
    t1 = db.trades.record_trade("AAPL", "buy", 10, 190.0)
    time.sleep(0.001)
    t2 = db.trades.record_trade("AAPL", "sell", 5, 195.0)

    listed = db.trades.list_recent()
    assert len(listed) == 2
    # Ordered DESC by executed_at — most recent first
    assert listed[0].id == t2.id
    assert listed[1].id == t1.id


def test_record_returns_trade_with_id_and_timestamp(temp_db: str) -> None:
    trade = db.trades.record_trade("AAPL", "buy", 10, 190.0)
    assert trade.id
    assert trade.executed_at
    assert trade.side == "buy"


def test_limit_caps_results(temp_db: str) -> None:
    for i in range(5):
        db.trades.record_trade("AAPL", "buy", 1, 100.0 + i)
    assert len(db.trades.list_recent(limit=3)) == 3
