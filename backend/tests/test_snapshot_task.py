"""Snapshot background task tests."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import db
from app.market import PriceCache
from app.portfolio import service
from app.snapshot_task import run_snapshot_loop


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    db_path = str(tmp_path / "snap.db")
    monkeypatch.setenv("FINALLY_DB_PATH", db_path)
    db.reset_initialization_state()
    db.ensure_initialized()
    yield db_path
    db.reset_initialization_state()
    if os.path.exists(db_path):
        os.remove(db_path)


async def test_loop_records_then_stops(temp_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a tiny interval, the loop should record at least one snapshot before stopping."""
    monkeypatch.setattr("app.snapshot_task.SNAPSHOT_INTERVAL_SECONDS", 0.05)

    cache = PriceCache()
    cache.update("AAPL", 100.0)
    stop = asyncio.Event()
    task = asyncio.create_task(run_snapshot_loop(cache, stop))

    await asyncio.sleep(0.18)
    stop.set()
    await task

    history = db.snapshots.list_history()
    assert len(history) >= 2
    # Initial portfolio is just $10k cash
    assert history[0].total_value == pytest.approx(10_000.0)


async def test_loop_picks_up_position_changes(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.snapshot_task.SNAPSHOT_INTERVAL_SECONDS", 0.05)

    cache = PriceCache()
    cache.update("AAPL", 100.0)
    service.execute_trade(cache, "AAPL", "buy", 1)
    cache.update("AAPL", 200.0)  # +100 unrealized

    stop = asyncio.Event()
    task = asyncio.create_task(run_snapshot_loop(cache, stop))
    await asyncio.sleep(0.12)
    stop.set()
    await task

    snaps = db.snapshots.list_history()
    assert any(s.total_value == pytest.approx(10_100.0) for s in snaps)
