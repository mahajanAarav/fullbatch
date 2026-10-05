"""A tiny in-memory rate limiter, so one person cannot burn through the shared free model quotas."""

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    """Allow at most `limit` calls per `window` seconds for each key (e.g. a user id)."""

    def __init__(self, limit: int, window: float, clock=time.monotonic):
        self._limit, self._window, self._clock = limit, window, clock
        self._calls: dict[object, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: object) -> bool:
        now = self._clock()
        with self._lock:
            calls = self._calls[key]
            while calls and now - calls[0] >= self._window:
                calls.popleft()
            if len(calls) >= self._limit:
                return False
            calls.append(now)
            return True

    def retry_after(self, key: object) -> int:
        """Seconds until the oldest counted call falls out of the window."""
        with self._lock:
            calls = self._calls[key]
            if not calls:
                return 0
            return max(1, int(self._window - (self._clock() - calls[0])) + 1)
