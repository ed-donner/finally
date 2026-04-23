"""Background task that snapshots total portfolio value on a fixed cadence."""

from __future__ import annotations

import asyncio
import logging

from . import db
from .market import PriceCache
from .portfolio import service

logger = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_SECONDS = 30
RETENTION_EVERY_N_SNAPSHOTS = 120  # Apply retention roughly hourly (120 * 30s)


async def run_snapshot_loop(cache: PriceCache, stop_event: asyncio.Event) -> None:
    """Loop until stop_event is set, recording one snapshot per interval."""
    iteration = 0
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SNAPSHOT_INTERVAL_SECONDS)
            return  # stop_event fired
        except asyncio.TimeoutError:
            pass

        try:
            value = service.total_value(cache)
            db.snapshots.record_snapshot(value)
            iteration += 1
            if iteration % RETENTION_EVERY_N_SNAPSHOTS == 0:
                result = db.snapshots.apply_retention()
                logger.info("snapshot retention: %s", result)
        except Exception:
            logger.exception("snapshot loop iteration failed")
