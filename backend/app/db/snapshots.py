"""Portfolio snapshot repository (P&L chart history) with retention policy."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .connection import connect
from .init import DEFAULT_USER_ID

# Retention policy (per PLAN.md §7):
#   - Full 30s resolution kept for the last 24 hours
#   - 24h–30d downsampled to one row per 5-minute bucket
#   - >30d deleted
RETENTION_FULL_RES_HOURS = 24
RETENTION_DOWNSAMPLE_BUCKET_MINUTES = 5
RETENTION_MAX_AGE_DAYS = 30


@dataclass(frozen=True)
class Snapshot:
    total_value: float
    recorded_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def record_snapshot(total_value: float, user_id: str = DEFAULT_USER_ID) -> Snapshot:
    snap = Snapshot(total_value=total_value, recorded_at=_now())
    with connect() as conn:
        conn.execute(
            "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), user_id, snap.total_value, snap.recorded_at),
        )
    return snap


def list_history(user_id: str = DEFAULT_USER_ID) -> list[Snapshot]:
    """Return snapshots ascending by time (suitable for direct chart rendering)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT total_value, recorded_at FROM portfolio_snapshots "
            "WHERE user_id = ? ORDER BY recorded_at ASC",
            (user_id,),
        ).fetchall()
        return [
            Snapshot(total_value=float(r["total_value"]), recorded_at=r["recorded_at"])
            for r in rows
        ]


def apply_retention(user_id: str = DEFAULT_USER_ID) -> dict[str, int]:
    """Enforce the retention policy. Returns counts {downsampled, deleted}.

    Strategy:
      1. Delete rows older than RETENTION_MAX_AGE_DAYS.
      2. For rows in [24h, 30d], keep only one per 5-minute bucket (oldest in bucket wins
         to preserve the earliest sample's timestamp; values within a 5-min bucket are
         within rounding noise for a daily P&L chart).
    """
    now = datetime.now(UTC)
    delete_before = (now - timedelta(days=RETENTION_MAX_AGE_DAYS)).isoformat()
    downsample_before = (now - timedelta(hours=RETENTION_FULL_RES_HOURS)).isoformat()
    bucket_seconds = RETENTION_DOWNSAMPLE_BUCKET_MINUTES * 60

    with connect() as conn:
        deleted_cursor = conn.execute(
            "DELETE FROM portfolio_snapshots WHERE user_id = ? AND recorded_at < ?",
            (user_id, delete_before),
        )
        deleted = deleted_cursor.rowcount

        # Downsample older-than-24h rows: keep the earliest row in each 5-minute bucket.
        # SQLite has no datetime arithmetic on ISO-8601 strings reliably, so do it in Python.
        rows = conn.execute(
            "SELECT id, recorded_at FROM portfolio_snapshots "
            "WHERE user_id = ? AND recorded_at < ? ORDER BY recorded_at ASC",
            (user_id, downsample_before),
        ).fetchall()

        seen_buckets: set[int] = set()
        to_delete: list[str] = []
        for row in rows:
            ts = datetime.fromisoformat(row["recorded_at"]).timestamp()
            bucket = int(ts // bucket_seconds)
            if bucket in seen_buckets:
                to_delete.append(row["id"])
            else:
                seen_buckets.add(bucket)

        if to_delete:
            placeholders = ",".join("?" * len(to_delete))
            conn.execute(
                f"DELETE FROM portfolio_snapshots WHERE id IN ({placeholders})",
                to_delete,
            )

    return {"deleted": deleted, "downsampled": len(to_delete)}
