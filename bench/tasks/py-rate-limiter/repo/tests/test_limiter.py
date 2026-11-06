import unittest

from limiter import RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class LimiterTest(unittest.TestCase):
    def test_burst_up_to_capacity(self):
        limiter = RateLimiter(1, 3, FakeClock())
        self.assertEqual([limiter.allow("a") for _ in range(4)], [True, True, True, False])
