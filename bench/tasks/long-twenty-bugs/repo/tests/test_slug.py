import unittest

from utils.slug import slug


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(slug('Hello,  World!') == 'hello-world')
