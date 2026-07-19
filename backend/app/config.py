"""Centralized configuration — loads .env once and exposes typed settings.

All values are optional so the app (and tests) run with no .env present:
the market simulator and LLM mock require no keys.
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # python-dotenv is a declared dep, but stay import-safe for bare envs
    from dotenv import load_dotenv

    # Load the repo-root .env (two levels up from this file: app/ -> backend/ -> repo)
    _repo_root = Path(__file__).resolve().parents[2]
    load_dotenv(_repo_root / ".env")
except Exception:  # pragma: no cover - dotenv missing or unreadable .env
    pass


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """Runtime settings, read fresh from the environment on access."""

    @property
    def openrouter_api_key(self) -> str:
        return os.environ.get("OPENROUTER_API_KEY", "").strip()

    @property
    def massive_api_key(self) -> str:
        return os.environ.get("MASSIVE_API_KEY", "").strip()

    @property
    def llm_mock(self) -> bool:
        return _flag("LLM_MOCK", default=False)

    @property
    def db_path(self) -> str:
        return os.environ.get("FINALLY_DB_PATH", "db/finally.db")

    @property
    def static_dir(self) -> str:
        """Directory holding the built frontend static export (prod)."""
        return os.environ.get("FINALLY_STATIC_DIR", "static")


settings = Settings()
