"""Thread-safe Token Bucket Rate Limiter implementation."""

import threading
import time


class TokenBucketRateLimiter:
    """
    A thread-safe Token Bucket rate limiter.

    Tokens are replenished at a fixed rate per second up to a maximum capacity.
    Callers can attempt to acquire tokens to enforce rate limits.
    """

    def __init__(self, rate_per_sec: float, capacity: int) -> None:
        """
        Initialize the rate limiter.

        :param rate_per_sec: Replenishment rate in tokens per second (must be > 0).
        :param capacity: Maximum token capacity of the bucket (must be > 0).
        :raises ValueError: If rate_per_sec or capacity is not positive.
        """
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be greater than zero")
        if capacity <= 0:
            raise ValueError("capacity must be greater than zero")

        self.rate_per_sec: float = float(rate_per_sec)
        self.capacity: int = int(capacity)
        self._tokens: float = float(capacity)
        self._last_refill: float = time.monotonic()
        self._lock: threading.Lock = threading.Lock()

    def _refill(self) -> None:
        """Refill tokens based on elapsed monotonic time."""
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_refill)
        if elapsed > 0.0:
            self._tokens = min(float(self.capacity), self._tokens + elapsed * self.rate_per_sec)
            self._last_refill = now

    def acquire(self, tokens: int = 1) -> bool:
        """
        Attempt to consume the specified number of tokens from the bucket.

        :param tokens: Number of tokens to acquire (default is 1).
        :return: True if sufficient tokens were available and consumed, False otherwise.
        :raises ValueError: If tokens is negative.
        """
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        if tokens == 0:
            return True

        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    @property
    def available_tokens(self) -> float:
        """
        Current token count in the bucket after accounting for replenishment.

        :return: Available tokens as a float.
        """
        with self._lock:
            self._refill()
            return self._tokens
