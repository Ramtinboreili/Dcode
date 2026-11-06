import unittest

from limiter import RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Hidden(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.limiter = RateLimiter(2, 4, self.clock)

    def test_keys_are_separate(self):
        for _ in range(4):
            self.assertTrue(self.limiter.allow("a"))
        self.assertFalse(self.limiter.allow("a"))
        self.assertTrue(self.limiter.allow("b"))

    def test_refill_is_capped(self):
        self.limiter.allow("a")
        self.clock.now += 1000
        self.assertEqual([self.limiter.allow("a") for _ in range(5)], [True, True, True, True, False])

    def test_refill_is_not_counted_twice(self):
        for _ in range(4):
            self.limiter.allow("a")
        self.clock.now += 1
        self.assertTrue(self.limiter.allow("a"))
        self.assertTrue(self.limiter.allow("a"))
        self.assertFalse(self.limiter.allow("a"))
        self.assertFalse(self.limiter.allow("a"))

    def test_a_refusal_costs_nothing(self):
        for _ in range(4):
            self.limiter.allow("a")
        for _ in range(10):
            self.assertFalse(self.limiter.allow("a"))
        self.clock.now += 0.5
        self.assertTrue(self.limiter.allow("a"))

    def test_exact_amount_is_allowed(self):
        self.assertTrue(self.limiter.allow("a", cost=4))
        self.assertFalse(self.limiter.allow("a", cost=1))

    def test_cost_over_capacity_never_passes(self):
        self.assertFalse(self.limiter.allow("a", cost=5))
        self.assertTrue(self.limiter.allow("a", cost=4))

    def test_retry_after(self):
        self.assertEqual(self.limiter.retry_after("new"), 0)
        for _ in range(4):
            self.limiter.allow("a")
        self.assertAlmostEqual(self.limiter.retry_after("a"), 0.5)
        self.assertAlmostEqual(self.limiter.retry_after("a", cost=3), 1.5)
        self.clock.now += 0.5
        self.assertEqual(self.limiter.retry_after("a"), 0)

    def test_retry_after_takes_nothing(self):
        self.limiter.retry_after("a", cost=2)
        self.assertEqual([self.limiter.allow("a") for _ in range(5)], [True, True, True, True, False])
