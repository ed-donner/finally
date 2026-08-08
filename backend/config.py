from __future__ import annotations

import os
from dataclasses import dataclass


def _clean(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # Market data
    massive_api_key: str
    massive_poll_seconds: float   # override poll cadence (paid tiers can go faster)
    sim_seed: int | None          # deterministic simulator for tests
    sse_push_seconds: float       # how often SSE flushes the cache to clients

    # LLM (documented here for completeness; owned by the chat agent)
    openrouter_api_key: str
    llm_mock: bool

    @property
    def use_massive(self) -> bool:
        return bool(self.massive_api_key)


def load_settings() -> Settings:
    return Settings(
        massive_api_key=_clean("MASSIVE_API_KEY"),
        massive_poll_seconds=float(_clean("MASSIVE_POLL_SECONDS", "15")),
        sim_seed=(int(_clean("SIM_SEED")) if _clean("SIM_SEED") else None),
        sse_push_seconds=float(_clean("SSE_PUSH_SECONDS", "0.5")),
        openrouter_api_key=_clean("OPENROUTER_API_KEY"),
        llm_mock=_clean("LLM_MOCK", "false").lower() == "true",
    )


settings = load_settings()
