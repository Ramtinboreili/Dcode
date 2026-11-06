import unittest

from utils.dedupe import dedupe


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(dedupe(['c', 'a', 'c', 'b', 'a']) == ['c', 'a', 'b'])
