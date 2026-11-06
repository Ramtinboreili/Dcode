import unittest

from utils.chunks import chunks


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(chunks([1, 2, 3], 2) == [[1, 2], [3]])
