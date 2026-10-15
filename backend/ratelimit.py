"""Brute-force protection for the credential endpoints.

In-process and deliberately simple. PostgreSQL is the wrong place to count
login attempts -- it would add a write to the hot path of a request that is
already deliberately slow -- and a shared cache is more machinery than a
single-process app needs.

The trade-off is stated plainly in the README: restarting the server clears
the counters, and running multiple workers gives each its own. That is fine
for one user's self-hosted instance, and it is the wrong tool if you put this
on the public internet.

Counters are keyed per client address *and* per username, because either one
alone is weak: limiting by address alone lets one attacker lock out every
account from behind a rotating proxy, and limiting by username alone lets one
client spray many names.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque

log = logging.getLogger("snapin.ratelimit")


class RateLimiter:
    """Fixed-window counter with exponential backoff on repeat offences."""

    def __init__(
        self,
        limit: int = 10,
        window_seconds: int = 300,
        lockout_multiplier: int = 8,
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self.lockout_multiplier = lockout_multiplier
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._strikes: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    @staticmethod
    def _key(scope: str, client: str, username: str = "") -> str:
        return f"{scope}:{client}:{username.lower()}"

    def _prune(self, now: float) -> None:
        """Drop timestamps outside the window so keys cannot grow forever."""
        if now - self._last_sweep < self.window:
            return
        self._last_sweep = now
        cutoff = now - self.window
        for key in list(self._attempts):
            bucket = self._attempts[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if not bucket:
                del self._attempts[key]
                self._strikes.pop(key, None)

    def check(self, scope: str, client: str, username: str = "") -> tuple[bool, int]:
        """Return (allowed, seconds_remaining). Does not count this call."""
        key = self._key(scope, client, username)
        now = time.monotonic()

        with self._lock:
            self._prune(now)

            strikes = self._strikes.get(key, 0)
            window = self.window * (1 + (self.lockout_multiplier - 1) * min(strikes, 4))
            bucket = self._attempts.get(key, deque())
            cutoff = now - window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()

            if len(bucket) >= self.limit:
                return False, int(window - (now - bucket[0])) + 1

            return True, 0

    def record_failure(self, scope: str, client: str, username: str = "") -> None:
        key = self._key(scope, client, username)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            self._attempts[key].append(now)

    def record_success(self, scope: str, client: str, username: str = "") -> None:
        """Clear the counter. Signing in should forgive earlier typos."""
        key = self._key(scope, client, username)
        with self._lock:
            self._attempts.pop(key, None)
            self._strikes.pop(key, None)

    def penalise(self, scope: str, client: str, username: str = "") -> None:
        """Count a failure that was clearly abusive, lengthening the window."""
        key = self._key(scope, client, username)
        with self._lock:
            self._strikes[key] += 1

    def strikes_count(self, scope: str, client: str, username: str = "") -> int:
        with self._lock:
            return self._strikes.get(self._key(scope, client, username), 0)

    def reset(self) -> None:
        with self._lock:
            self._attempts.clear()
            self._strikes.clear()


# Separate limiters: registering less often than signing in is reasonable,
# since sign-up is rarer and more abusable.
login_limiter = RateLimiter(limit=10, window_seconds=300)
register_limiter = RateLimiter(limit=5, window_seconds=600)
link_limiter = RateLimiter(limit=5, window_seconds=600)


def client_address(request) -> str:
    """Identify the caller for rate limiting.

    ``X-Forwarded-For`` is honoured because the app is expected to sit behind a
    reverse proxy in deployment. That header is trivially spoofable, so treat
    these limits as guidance against casual abuse, not a security boundary --
    which is why this module says so in its docstring.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"