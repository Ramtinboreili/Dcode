import unittest
from datetime import date

from report import days_between, parse, total


class Hidden(unittest.TestCase):
    def test_single_day(self):
        self.assertEqual(days_between(date(2024, 2, 29), date(2024, 2, 29)), [date(2024, 2, 29)])

    def test_empty_when_reversed(self):
        self.assertEqual(days_between(date(2024, 3, 2), date(2024, 3, 1)), [])

    def test_month_edge(self):
        days = days_between(date(2024, 2, 28), date(2024, 3, 1))
        self.assertEqual(days, [date(2024, 2, 28), date(2024, 2, 29), date(2024, 3, 1)])

    def test_total_includes_both_ends(self):
        sales = {date(2025, 1, 1): 1, date(2025, 1, 2): 2, date(2025, 1, 3): 4, date(2025, 1, 4): 8}
        self.assertEqual(total(sales, date(2025, 1, 2), date(2025, 1, 3)), 6)

    def test_parse_still_works(self):
        self.assertEqual(parse("2025-01-09"), date(2025, 1, 9))
