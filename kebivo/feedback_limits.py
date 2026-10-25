"""Process-local fixed-window feedback counters (reset on process restart)."""

import math
import threading
import time
from collections.abc import Hashable
from typing import Any

from fastapi import HTTPException, Request

from kebivo.auth import _peer_ip


class FixedWindowLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, Hashable], tuple[int, int]] = {}
        self._window: int | None = None

    def check(self, kind: str, key: Hashable, limit: int) -> None:
        now = time.monotonic()
        window = math.floor(now / 60)
        with self._lock:
            if self._window != window:
                self._counts.clear()
                self._window = window
            old_window, count = self._counts.get((kind, key), (window, 0))
            if old_window != window:
                count = 0
            if count >= limit:
                retry_after = max(1, math.ceil((window + 1) * 60 - now))
                raise HTTPException(
                    status_code=429,
                    detail="Feedback rate limit exceeded",
                    headers={"Retry-After": str(retry_after)},
                )
            self._counts[(kind, key)] = (window, count + 1)


def check_feedback_rate_limit(request: Request, token_id: str) -> None:
    settings: Any = request.app.state.settings
    limiter: FixedWindowLimiter = request.app.state.feedback_limiter
    limiter.check("ip", _peer_ip(request), settings.feedback_rate_limit_per_ip)
    limiter.check("token", token_id, settings.feedback_rate_limit_per_token)


def record_feedback_auth_failure(request: Request) -> None:
    settings: Any = request.app.state.settings
    limiter: FixedWindowLimiter = request.app.state.feedback_limiter
    limiter.check("ip", _peer_ip(request), settings.feedback_rate_limit_per_ip)
