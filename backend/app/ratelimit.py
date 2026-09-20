"""In-memory per-IP rate limiting for the expensive/mutating endpoints (sliding window)."""

from __future__ import annotations

import math
import os
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

WINDOW_SECONDS = 3600.0
DEFAULT_CHAT_PER_HOUR = 15
DEFAULT_TRADES_PER_HOUR = 20


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def client_ip(request: Request) -> str:
    # Behind a proxy (Railway) the peer is the proxy itself. The proxy appends the address it saw
    # to X-Forwarded-For, so the last entry is trustworthy; earlier ones can be client-supplied.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """Sliding-window limiter keyed by (bucket, ip). State is per process (single replica)."""

    def __init__(self, rules: dict[str, tuple[frozenset[str], int]], window: float = WINDOW_SECONDS):
        # rules: bucket name -> (paths, max requests per window)
        self.window = window
        self._path_to_bucket = {p: b for b, (paths, _) in rules.items() for p in paths}
        self._limits = {b: limit for b, (_, limit) in rules.items()}
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def check(self, path: str, ip: str, now: float | None = None) -> int | None:
        """Record a hit. Returns None if allowed, else seconds until the next slot frees up."""
        bucket = self._path_to_bucket.get(path)
        if bucket is None:
            return None
        now = time.monotonic() if now is None else now
        hits = self._hits[(bucket, ip)]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self._limits[bucket]:
            return max(1, math.ceil(self.window - (now - hits[0])))
        hits.append(now)
        return None


def build_limiter() -> RateLimiter:
    chat = _env_int("RATE_LIMIT_CHAT_PER_HOUR", DEFAULT_CHAT_PER_HOUR)
    trades = _env_int("RATE_LIMIT_TRADES_PER_HOUR", DEFAULT_TRADES_PER_HOUR)
    return RateLimiter(
        {
            "chat": (frozenset({"/api/chat"}), chat),
            # Both public paths of the same trade handler share one budget.
            "trade": (frozenset({"/api/trade", "/api/portfolio/trade"}), trades),
        }
    )


def install_rate_limit(app, limiter: RateLimiter) -> None:
    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        if request.method == "POST":
            retry_after = limiter.check(request.url.path, client_ip(request))
            if retry_after is not None:
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": f"Rate limit exceeded. Try again in {retry_after} seconds.",
                        "retry_after": retry_after,
                    },
                    headers={"Retry-After": str(retry_after)},
                )
        return await call_next(request)
