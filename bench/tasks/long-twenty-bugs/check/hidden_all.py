import unittest

from utils.clamp import clamp
from utils.chunks import chunks
from utils.median import median
from utils.flatten import flatten
from utils.dedupe import dedupe
from utils.wordcount import word_count
from utils.leap import is_leap
from utils.roman import to_roman
from utils.caesar import caesar
from utils.merge import merge_sorted
from utils.bisect_find import find
from utils.kvparse import parse_kv
from utils.moving import moving_average
from utils.bytesize import human_bytes
from utils.truncate import truncate
from utils.invert import invert
from utils.rle import rle
from utils.percent import percent
from utils.slug import slug
from utils.rangeparse import parse_ranges


class Hidden(unittest.TestCase):
    def test_clamp(self):
        self.assertTrue(clamp(5, 0, 3) == 3)
        self.assertTrue(clamp(-1, 0, 3) == 0)
        self.assertTrue(clamp(2, 0, 3) == 2)

    def test_chunks(self):
        self.assertTrue(chunks([1, 2, 3], 2) == [[1, 2], [3]])
        self.assertTrue(chunks([1, 2], 2) == [[1, 2]])
        self.assertTrue(chunks([], 2) == [])

    def test_median(self):
        self.assertTrue(median([1, 2, 3, 4]) == 2.5)
        self.assertTrue(median([3, 1, 2]) == 2)

    def test_flatten(self):
        self.assertTrue(flatten([1, [2, [3, [4]]]]) == [1, 2, 3, 4])
        self.assertTrue(flatten([]) == [])

    def test_dedupe(self):
        self.assertTrue(dedupe(['c', 'a', 'c', 'b', 'a']) == ['c', 'a', 'b'])
        self.assertTrue(dedupe([3, 1, 3, 2, 1, 9, 9]) == [3, 1, 2, 9])

    def test_wordcount(self):
        self.assertTrue(word_count('The the  THE\ncat') == {'the': 3, 'cat': 1})
        self.assertTrue(word_count('') == {})

    def test_leap(self):
        self.assertTrue(not is_leap(1900))
        self.assertTrue(is_leap(2000))
        self.assertTrue(is_leap(2024))
        self.assertTrue(not is_leap(2023))

    def test_roman(self):
        self.assertTrue(to_roman(4) == 'IV')
        self.assertTrue(to_roman(1994) == 'MCMXCIV')
        self.assertTrue(to_roman(3) == 'III')
        self.assertTrue(to_roman(49) == 'XLIX')

    def test_caesar(self):
        self.assertTrue(caesar('xyz', 3) == 'abc')
        self.assertTrue(caesar('abc', -1) == 'zab')
        self.assertTrue(caesar('A b', 1) == 'A c')

    def test_merge(self):
        self.assertTrue(merge_sorted([1, 3, 9], [2]) == [1, 2, 3, 9])
        self.assertTrue(merge_sorted([], [1]) == [1])
        self.assertTrue(merge_sorted([1, 1], [1]) == [1, 1, 1])

    def test_bisect_find(self):
        self.assertTrue(find([1, 3, 5], 5) == 2)
        self.assertTrue(find([1], 1) == 0)
        self.assertTrue(find([1, 3, 5], 1) == 0)
        self.assertTrue(find([1, 3, 5], 4) == -1)
        self.assertTrue(find([], 4) == -1)

    def test_kvparse(self):
        self.assertTrue(parse_kv('a=1; token=x=y=') == {'a': '1', 'token': 'x=y='})
        self.assertTrue(parse_kv('a=1;') == {'a': '1'})
        self.assertTrue(parse_kv('') == {})

    def test_moving(self):
        self.assertTrue(moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5])
        self.assertTrue(moving_average([1, 2], 3) == [])

    def test_bytesize(self):
        self.assertTrue(human_bytes(1024) == '1.0 KB')
        self.assertTrue(human_bytes(1) == '1.0 B')
        self.assertTrue(human_bytes(1024 ** 4) == '1.0 TB')

    def test_truncate(self):
        self.assertTrue(truncate('abcdefgh', 6) == 'abc...')
        self.assertTrue(len(truncate('x' * 50, 10)) == 10)
        self.assertTrue(truncate('abc', 3) == 'abc')

    def test_invert(self):
        self.assertTrue(invert({'a': 1, 'b': 1}) == {1: ['a', 'b']})
        self.assertTrue(invert({}) == {})
        self.assertTrue(invert({'a': 1, 'c': 2}) == {1: ['a'], 2: ['c']})

    def test_rle(self):
        self.assertTrue(rle('aab') == [('a', 2), ('b', 1)])
        self.assertTrue(rle('') == [])
        self.assertTrue(rle('aaa') == [('a', 3)])

    def test_percent(self):
        self.assertTrue(percent(0, 0) == 0)
        self.assertTrue(percent(1, 4) == 25)

    def test_slug(self):
        self.assertTrue(slug('Hello,  World!') == 'hello-world')
        self.assertTrue(slug('--a--') == 'a')

    def test_rangeparse(self):
        self.assertTrue(parse_ranges('1-3,7') == [1, 2, 3, 7])
        self.assertTrue(parse_ranges('5') == [5])
        self.assertTrue(parse_ranges('2-2') == [2])
