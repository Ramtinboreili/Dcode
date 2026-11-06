"""A token bucket per key. `rate` tokens are added each second, up to `capacity`."""
import time


class Bucket:
    def __init__(self, tokens, updated):
        self.tokens = tokens
        self.updated = updated


class RateLimiter:
    def __init__(self, rate, capacity, clock=time.monotonic):
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self.rate = rate
        self.capacity = capacity
        self.clock = clock
        self.buckets = {}

    def _bucket(self, key):
        now = self.clock()
        bucket = self.buckets.get(key)
        if bucket is None:
            bucket = self.buckets.setdefault("default", Bucket(0, now))
        bucket.tokens = bucket.tokens + (now - bucket.updated) * self.rate
        return bucket

    def allow(self, key, cost=1):
        """Take `cost` tokens if there are enough. True when the request may go ahead."""
        bucket = self._bucket(key)
        bucket.tokens -= cost
        return bucket.tokens > 0
