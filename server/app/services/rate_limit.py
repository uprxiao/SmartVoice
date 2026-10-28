from __future__ import annotations

import hashlib
import math
from time import time as utc_seconds


class FixedWindowRateLimiter:
    """Single-worker UTC epoch-aligned windows; only retain the current window.

    Checking and consuming are synchronous, so they cannot interleave within
    this application's one ASGI event loop. Rejected requests do not extend
    the window. This is resource protection, not persistent abuse tracking.
    """

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window_seconds = window_seconds
        self.window_number: int | None = None
        self.counts: dict[str, int] = {}

    def retry_after(self, key: str, now: float) -> int | None:
        window = math.floor(now / self.window_seconds)
        if window != self.window_number:
            self.counts.clear()
            self.window_number = window
        if self.counts.get(key, 0) >= self.limit:
            return max(1, math.ceil((window + 1) * self.window_seconds - now))
        return None

    def record(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def check(self, key: str) -> int | None:
        retry = self.retry_after(key, utc_seconds())
        if retry is None:
            self.record(key)
        return retry

    def reset(self) -> None:
        self.window_number = None
        self.counts.clear()


class LoginRateLimiter:
    """10/IP/UTC minute AND 20/normalized-email digest/UTC ten minutes.

    Successful and unsuccessful credential checks consume the same budgets;
    an already throttled attempt consumes neither independent bucket.
    """

    def __init__(self, ip_limit: int = 10, email_limit: int = 20):
        self.ip_limiter = FixedWindowRateLimiter(ip_limit, 60)
        self.email_limiter = FixedWindowRateLimiter(email_limit, 600)

    @staticmethod
    def email_digest(email: str) -> str:
        return hashlib.sha256(email.encode("utf-8")).hexdigest()

    def check(self, ip: str, email: str) -> int | None:
        now = utc_seconds()
        digest = self.email_digest(email)
        retry = max(self.ip_limiter.retry_after(ip, now) or 0,
                    self.email_limiter.retry_after(digest, now) or 0)
        if retry:
            return retry
        self.ip_limiter.record(ip)
        self.email_limiter.record(digest)
        return None

    def reset(self) -> None:
        self.ip_limiter.reset()
        self.email_limiter.reset()
