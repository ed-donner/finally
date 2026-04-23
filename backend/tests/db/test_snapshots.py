"""Portfolio snapshot tests including retention policy."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app import db
from app.db.connection import connect


def _insert_snapshot_at(when: datetime, value: float) -> None:
    """Bypass record_snapshot() so we can backdate rows for retention tests."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), db.DEFAULT_USER_ID, value, when.isoformat()),
        )


def test_record_and_list(temp_db: str) -> None:
    baseline = db.snapshots.list_history()
    db.snapshots.record_snapshot(10_000.0)
    db.snapshots.record_snapshot(10_500.0)
    history = db.snapshots.list_history()
    assert [s.total_value for s in history[-2:]] == [10_000.0, 10_500.0]
    assert len(history) == len(baseline) + 2


def test_list_history_orders_ascending(temp_db: str) -> None:
    now = datetime.now(UTC)
    _insert_snapshot_at(now - timedelta(minutes=5), 100.0)
    _insert_snapshot_at(now - timedelta(minutes=1), 200.0)
    _insert_snapshot_at(now - timedelta(minutes=3), 150.0)
    history = db.snapshots.list_history()
    assert [s.total_value for s in history[:-1]] == [100.0, 150.0, 200.0]
    assert history[-1].total_value == db.DEFAULT_CASH_BALANCE


def test_retention_deletes_rows_older_than_max_age(temp_db: str) -> None:
    now = datetime.now(UTC)
    _insert_snapshot_at(now - timedelta(days=31), 1.0)
    _insert_snapshot_at(now - timedelta(days=29), 2.0)
    _insert_snapshot_at(now - timedelta(hours=1), 3.0)
    result = db.snapshots.apply_retention()
    assert result["deleted"] == 1
    values = [s.total_value for s in db.snapshots.list_history()]
    assert 1.0 not in values
    assert {2.0, 3.0}.issubset(set(values))


def test_retention_downsamples_older_than_24h(temp_db: str) -> None:
    # 5 snapshots > 24h old, deterministically packed into one 5-minute bucket.
    # The bucket boundary is on multiples of 300s — snap to one and offset by 60s
    # so all 5 samples (spanning 0..40s) land inside the same bucket regardless
    # of when the test runs.
    bucket_seconds = 300
    rough = datetime.now(UTC) - timedelta(hours=25)
    aligned_ts = (int(rough.timestamp()) // bucket_seconds) * bucket_seconds + 60
    base = datetime.fromtimestamp(aligned_ts, UTC)
    for i in range(5):
        _insert_snapshot_at(base + timedelta(seconds=10 * i), 100.0 + i)

    # Plus a recent (within 24h) sample that must be untouched
    _insert_snapshot_at(datetime.now(UTC) - timedelta(minutes=1), 999.0)

    result = db.snapshots.apply_retention()
    assert result["downsampled"] == 4
    history = db.snapshots.list_history()
    assert len(history) == 3
    assert history[0].total_value == 100.0  # earliest in bucket survives
    assert history[1].total_value == 999.0
    assert history[2].total_value == db.DEFAULT_CASH_BALANCE


def test_retention_preserves_full_resolution_within_24h(temp_db: str) -> None:
    base = datetime.now(UTC) - timedelta(hours=1)
    for i in range(5):
        _insert_snapshot_at(base + timedelta(seconds=30 * i), 100.0 + i)
    db.snapshots.apply_retention()
    assert len(db.snapshots.list_history()) == 6
