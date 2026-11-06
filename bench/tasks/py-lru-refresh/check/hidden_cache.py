import random
import unittest
from collections import OrderedDict

from cache import LRUCache


class Hidden(unittest.TestCase):
    def test_oldest_goes_first(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        self.assertEqual(cache.keys(), ["b", "c"])

    def test_put_refreshes(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("a", 9)
        cache.put("c", 3)
        self.assertEqual(cache.get("a"), 9)
        self.assertIsNone(cache.get("b"))

    def test_delete_keeps_things_consistent(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.delete("a")
        self.assertEqual(cache.keys(), ["b"])
        cache.put("c", 3)
        cache.put("d", 4)
        self.assertEqual(cache.keys(), ["c", "d"])
        self.assertEqual(len(cache), 2)

    def test_counts(self):
        cache = LRUCache(1)
        cache.put("a", 1)
        cache.get("a")
        cache.get("z")
        self.assertEqual((cache.hits, cache.misses), (1, 1))

    def test_against_a_model(self):
        rng = random.Random(7)
        cache, model = LRUCache(5), OrderedDict()
        for _ in range(3000):
            key = rng.randrange(12)
            if rng.random() < 0.5:
                cache.put(key, key)
                model[key] = key
                model.move_to_end(key)
                if len(model) > 5:
                    model.popitem(last=False)
            else:
                expected = model.get(key)
                if key in model:
                    model.move_to_end(key)
                self.assertEqual(cache.get(key), expected)
            self.assertEqual(cache.keys(), list(model))
