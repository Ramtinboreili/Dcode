import unittest

from duration import format_duration, parse_duration


class Hidden(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_duration("2d3h4m5s"), 2 * 86400 + 3 * 3600 + 4 * 60 + 5)
        self.assertEqual(parse_duration(" 10m "), 600)

    def test_rejects_junk(self):
        for bad in ("", "abc", "10", "5x", "1h junk"):
            with self.assertRaises(ValueError, msg=bad):
                parse_duration(bad)

    def test_format(self):
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(90061), "1d1h1m1s")
        self.assertEqual(format_duration(59), "59s")

    def test_round_trip(self):
        for value in (1, 61, 3600, 86399, 100000):
            self.assertEqual(parse_duration(format_duration(value)), value)
