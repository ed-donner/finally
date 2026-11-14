"""Application configuration: loads `.env` from the repo root and exposes paths/settings.

Import this module early (``app.main`` does) so environment variables from `.env`
are visible to every other module (market data factory, LLM client, db).
Existing environment variables always win over `.env` values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent  # .../backend
REPO_ROOT = BACKEND_DIR.parent  # .../finally
ENV_FILE = REPO_ROOT / ".env"

SNAPSHOT_INTERVAL_SECONDS = 30.0


def load_env() -> None:
    """Load `.env` from the repo root (no-op if missing). Does not override real env vars."""
    if ENV_FILE.is_file():
        load_dotenv(ENV_FILE, override=False)


load_env()


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str
    massive_api_key: str
    llm_mock: bool
    db_path: Path
    static_dir: Path
    snapshot_interval: float


def get_settings() -> Settings:
    """Read settings from the environment at call time (tests may monkeypatch env vars)."""
    db_env = os.environ.get("FINALLY_DB_PATH", "").strip()
    static_env = os.environ.get("FINALLY_STATIC_DIR", "").strip()
    interval_env = os.environ.get("FINALLY_SNAPSHOT_INTERVAL", "").strip()
    try:
        interval = float(interval_env) if interval_env else SNAPSHOT_INTERVAL_SECONDS
    except ValueError:
        interval = SNAPSHOT_INTERVAL_SECONDS
    return Settings(
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", "").strip(),
        massive_api_key=os.environ.get("MASSIVE_API_KEY", "").strip(),
        llm_mock=_bool_env("LLM_MOCK"),
        db_path=Path(db_env) if db_env else REPO_ROOT / "db" / "finally.db",
        static_dir=Path(static_env) if static_env else BACKEND_DIR / "static",
        snapshot_interval=max(interval, 1.0),
    )
