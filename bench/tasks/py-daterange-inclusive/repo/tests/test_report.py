import unittest
from datetime import date

from report import days_between, total


class ReportTest(unittest.TestCase):
    def test_days(self):
        self.assertEqual(len(days_between(date(2025, 1, 1), date(2025, 1, 3))), 3)

    def test_total(self):
        sales = {date(2025, 1, 1): 5, date(2025, 1, 2): 7}
        self.assertEqual(total(sales, date(2025, 1, 1), date(2025, 1, 2)), 12)
