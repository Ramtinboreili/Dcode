import unittest

from utils.wordcount import word_count


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(word_count('The the  THE\ncat') == {'the': 3, 'cat': 1})
