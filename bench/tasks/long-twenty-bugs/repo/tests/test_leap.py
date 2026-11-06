import unittest

from utils.leap import is_leap


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(not is_leap(1900))
        self.assertTrue(is_leap(2000))
