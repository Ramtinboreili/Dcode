import unittest

from pager import page_count, paginate


class PagerTest(unittest.TestCase):
    def test_first_page(self):
        self.assertEqual(paginate([1, 2, 3, 4, 5], 1, 2), [1, 2])

    def test_last_page_is_short(self):
        self.assertEqual(paginate([1, 2, 3, 4, 5], 3, 2), [5])

    def test_page_count_rounds_up(self):
        self.assertEqual(page_count(5, 2), 3)


if __name__ == "__main__":
    unittest.main()
