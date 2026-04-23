"""Positions repository tests."""

from __future__ import annotations

from app import db


def test_no_positions_initially(temp_db: str) -> None:
    assert db.positions.list_positions() == []
    assert db.positions.get_position("AAPL") is None


def test_upsert_inserts_then_updates(temp_db: str) -> None:
    db.positions.upsert_position("AAPL", quantity=10, avg_cost=190.0)
    pos = db.positions.get_position("AAPL")
    assert pos is not None
    assert pos.quantity == 10
    assert pos.avg_cost == 190.0

    db.positions.upsert_position("AAPL", quantity=15, avg_cost=185.0)
    pos = db.positions.get_position("AAPL")
    assert pos is not None
    assert pos.quantity == 15
    assert pos.avg_cost == 185.0
    # Upsert must not create a duplicate row
    assert len(db.positions.list_positions()) == 1


def test_list_positions_excludes_zero_quantity(temp_db: str) -> None:
    db.positions.upsert_position("AAPL", quantity=10, avg_cost=190.0)
    db.positions.upsert_position("GOOGL", quantity=0, avg_cost=175.0)
    tickers = [p.ticker for p in db.positions.list_positions()]
    assert tickers == ["AAPL"]


def test_delete_position(temp_db: str) -> None:
    db.positions.upsert_position("AAPL", quantity=10, avg_cost=190.0)
    db.positions.delete_position("AAPL")
    assert db.positions.get_position("AAPL") is None


def test_ticker_normalized_to_uppercase(temp_db: str) -> None:
    db.positions.upsert_position("aapl", quantity=10, avg_cost=190.0)
    assert db.positions.get_position("AAPL") is not None
