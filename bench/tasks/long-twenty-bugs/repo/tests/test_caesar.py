import unittest

from utils.caesar import caesar


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(caesar('xyz', 3) == 'abc')
