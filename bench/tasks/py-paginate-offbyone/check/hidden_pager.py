import unittest

from pager import page_count, paginate


class Hidden(unittest.TestCase):
    def test_pages(self):
        items = list(range(10))
        self.assertEqual(paginate(items, 1, 3), [0, 1, 2])
        self.assertEqual(paginate(items, 2, 3), [3, 4, 5])
        self.assertEqual(paginate(items, 4, 3), [9])
        self.assertEqual(paginate(items, 5, 3), [])

    def test_count(self):
        self.assertEqual(page_count(0, 3), 0)
        self.assertEqual(page_count(9, 3), 3)
        self.assertEqual(page_count(10, 3), 4)
        self.assertEqual(page_count(1, 3), 1)
