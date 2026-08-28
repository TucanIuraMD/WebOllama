"""Security helpers: rate limiting and CSRF protection."""
import logging
import secrets
import threading
import time

logger = logging.getLogger(__name__)

# In-memory sliding-window rate limiter keyed by (bucket, client_id)
_rate = {}
_rate_lock = threading.Lock()


class RateLimiter:
    """Sliding window rate limiter."""

    def __init__(self, max_events: int, window: float) -> None:
        self.max_events = max_events
        self.window = window

    def allow(self, key: str) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with _rate_lock:
            bucket = _rate.setdefault(key, [])
            # drop events older than window
            cutoff = now - self.window
            bucket[:] = [t for t in bucket if t > cutoff]
            if len(bucket) >= self.max_events:
                retry = max(0.0, self.window - (now - bucket[0]))
                return False, int(retry) + 1
            bucket.append(now)
            return True, 0

    def reset(self, key: str) -> None:
        with _rate_lock:
            _rate.pop(key, None)


def make_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_ok(header_token: str | None, cookie_token: str | None) -> bool:
    """Compare CSRF token from X-CSRF-Token header with the session cookie token."""
    if not header_token or not cookie_token:
        return False
    return secrets.compare_digest(header_token, cookie_token)
