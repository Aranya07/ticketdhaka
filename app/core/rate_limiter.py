"""Token-bucket rate limiter.

Each client gets a bucket holding up to `capacity` tokens. Tokens refill
continuously at `refill_per_sec`. A request spends 1 token; when the bucket is
empty, the request is rejected with 429. This allows short bursts but caps the
sustained rate. Refill is computed lazily on each request, so there's no timer thread.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    def __init__(self, capacity: int, refill_per_sec: float, max_clients: int = 10_000) -> None:
        self.capacity = capacity
        self.refill = refill_per_sec
        self.max_clients = max_clients
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, client_id: str, now: float | None = None) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._buckets.get(client_id)
            if bucket is None:
                if len(self._buckets) >= self.max_clients:
                    self._buckets.clear()  # crude memory bound; fine for a single instance
                bucket = _Bucket(tokens=self.capacity, updated_at=now)
                self._buckets[client_id] = bucket

            elapsed = now - bucket.updated_at
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill)
            bucket.updated_at = now

            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return True, 0.0
            return False, (1 - bucket.tokens) / self.refill
