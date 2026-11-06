import unittest

from utils.truncate import truncate


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(truncate('abcdefgh', 6) == 'abc...')
