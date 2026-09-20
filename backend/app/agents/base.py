"""BaseAgent: identity, DB handle and audit logging shared by all agents."""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from ..db import Database

logger = logging.getLogger(__name__)

# One correlation id per API request; every agent call in it is logged under the same id.
# ContextVar (not a global) so concurrent requests each keep their own id.
correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")

# Tolerance for float comparisons on share quantities and cash (avoids 0.30000000000000004 issues).
# Shared by Risk (approve) and Portfolio (re-check) so the two can never disagree.
EPS = 1e-9

# Single-user app: every row uses this id. Kept in one place so multi-user is a small change later.
USER_ID = "default"


def new_id() -> str:
    return str(uuid.uuid4())


# ISO-8601 UTC with millisecond precision; matches the format used by schema.sql defaults.
def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# Convert pydantic models (possibly nested in lists/dicts) into plain JSON-serializable data.
def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


class BaseAgent:
    # Stable identifier written to agent_logs.agent_id (and trades.agent_id for the Portfolio agent).
    agent_id: str = "base"

    def __init__(self, db: Database) -> None:
        self.db = db

    def log(
        self,
        action: str,
        status: str,
        input: Any = None,
        output: Any = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Write one agent_logs row. Auditing must never break a request, so failures are swallowed."""
        try:
            self.db.execute(
                "INSERT INTO agent_logs (id, user_id, correlation_id, agent_id, action, status,"
                " input, output, error, duration_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id(),
                    USER_ID,
                    correlation_id.get() or new_id(),
                    self.agent_id,
                    action,
                    status,
                    json.dumps(_jsonable(input), default=str) if input is not None else None,
                    json.dumps(_jsonable(output), default=str) if output is not None else None,
                    error,
                    duration_ms,
                    now_iso(),
                ),
            )
        except Exception:  # noqa: BLE001
            # A logging failure must never fail the trade or chat request, but it must not be silent.
            logger.warning("agent_logs write failed for %s.%s", self.agent_id, action, exc_info=True)

    @contextmanager
    def timed(self):
        """Yields a callable returning elapsed milliseconds."""
        start = time.perf_counter()
        yield lambda: int((time.perf_counter() - start) * 1000)
