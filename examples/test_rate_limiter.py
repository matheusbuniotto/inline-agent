"""Unit tests for TokenBucketRateLimiter."""

import threading
import unittest
from unittest.mock import patch

from rate_limiter import TokenBucketRateLimiter


class TestTokenBucketRateLimiter(unittest.TestCase):
    def test_initialization(self) -> None:
        limiter = TokenBucketRateLimiter(rate_per_sec=10.0, capacity=20)
        self.assertEqual(limiter.rate_per_sec, 10.0)
        self.assertEqual(limiter.capacity, 20)
        self.assertEqual(limiter.available_tokens, 20.0)

    def test_invalid_initialization(self) -> None:
        with self.assertRaises(ValueError):
            TokenBucketRateLimiter(rate_per_sec=0.0, capacity=10)
        with self.assertRaises(ValueError):
            TokenBucketRateLimiter(rate_per_sec=-5.0, capacity=10)
        with self.assertRaises(ValueError):
            TokenBucketRateLimiter(rate_per_sec=10.0, capacity=0)
        with self.assertRaises(ValueError):
            TokenBucketRateLimiter(rate_per_sec=10.0, capacity=-1)

    def test_acquire_default_token(self) -> None:
        limiter = TokenBucketRateLimiter(rate_per_sec=1.0, capacity=5)
        self.assertTrue(limiter.acquire())
        self.assertAlmostEqual(limiter.available_tokens, 4.0, places=1)

    def test_acquire_multiple_tokens(self) -> None:
        limiter = TokenBucketRateLimiter(rate_per_sec=1.0, capacity=5)
        self.assertTrue(limiter.acquire(3))
        self.assertAlmostEqual(limiter.available_tokens, 2.0, places=1)
        self.assertFalse(limiter.acquire(3))
        self.assertTrue(limiter.acquire(2))
        self.assertFalse(limiter.acquire(1))

    def test_acquire_zero_tokens(self) -> None:
        limiter = TokenBucketRateLimiter(rate_per_sec=1.0, capacity=5)
        self.assertTrue(limiter.acquire(0))
        self.assertAlmostEqual(limiter.available_tokens, 5.0, places=1)

    def test_acquire_negative_tokens(self) -> None:
        limiter = TokenBucketRateLimiter(rate_per_sec=1.0, capacity=5)
        with self.assertRaises(ValueError):
            limiter.acquire(-1)

    def test_acquire_exceeding_capacity(self) -> None:
        limiter = TokenBucketRateLimiter(rate_per_sec=10.0, capacity=5)
        self.assertFalse(limiter.acquire(6))

    @patch("time.monotonic")
    def test_token_refill_over_time(self, mock_time) -> None:
        mock_time.return_value = 100.0
        limiter = TokenBucketRateLimiter(rate_per_sec=2.0, capacity=10)

        self.assertTrue(limiter.acquire(10))
        self.assertEqual(limiter.available_tokens, 0.0)

        # Advance time by 1.5 seconds -> 3.0 tokens refilled
        mock_time.return_value = 101.5
        self.assertAlmostEqual(limiter.available_tokens, 3.0, places=5)

        # Consume 2 tokens -> 1.0 remaining
        self.assertTrue(limiter.acquire(2))
        self.assertAlmostEqual(limiter.available_tokens, 1.0, places=5)

    @patch("time.monotonic")
    def test_bucket_capacity_cap(self, mock_time) -> None:
        mock_time.return_value = 100.0
        limiter = TokenBucketRateLimiter(rate_per_sec=5.0, capacity=10)

        # Advance time significantly beyond what is needed to fill capacity
        mock_time.return_value = 300.0
        self.assertEqual(limiter.available_tokens, 10.0)

    def test_thread_safety_concurrent_acquisitions(self) -> None:
        capacity = 100
        # Negligible refill rate to verify exact token allocation under concurrency
        limiter = TokenBucketRateLimiter(rate_per_sec=0.00001, capacity=capacity)
        successes: list[bool] = []
        threads: list[threading.Thread] = []

        def worker() -> None:
            if limiter.acquire(1):
                successes.append(True)

        for _ in range(200):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(len(successes), capacity)
        self.assertAlmostEqual(limiter.available_tokens, 0.0, delta=0.01)


if __name__ == "__main__":
    unittest.main()