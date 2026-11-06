import unittest

from duration import format_duration, parse_duration


class DurationTest(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(parse_duration("45s"), 45)

    def test_combined(self):
        self.assertEqual(parse_duration("1h30m"), 5400)

    def test_format(self):
        self.assertEqual(format_duration(5400), "1h30m")
