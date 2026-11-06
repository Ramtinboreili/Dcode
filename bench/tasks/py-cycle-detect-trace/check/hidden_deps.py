import unittest

import deps


class Hidden(unittest.TestCase):
    def test_order(self):
        graph = {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}
        order = deps.resolve(graph, "a")
        self.assertEqual(sorted(order), ["a", "b", "c", "d"], "each plugin once")
        self.assertLess(order.index("d"), order.index("b"))
        self.assertLess(order.index("d"), order.index("c"))
        self.assertEqual(order[-1], "a")

    def test_unknown_is_a_leaf(self):
        self.assertEqual(deps.resolve({"a": ["z"]}, "a"), ["z", "a"])

    def test_cycle_error_names_it(self):
        graph = {"alpha": ["beta"], "beta": ["gamma"], "gamma": ["alpha"], "other": []}
        with self.assertRaises(deps.CycleError) as caught:
            deps.resolve(graph, "alpha")
        for name in ("alpha", "beta", "gamma"):
            self.assertIn(name, str(caught.exception))
        self.assertNotIn("other", str(caught.exception))

    def test_self_cycle(self):
        with self.assertRaises(deps.CycleError):
            deps.resolve({"a": ["a"]}, "a")

    def test_diamond_isnt_a_cycle(self):
        graph = {"a": ["b", "c"], "b": ["c"], "c": []}
        self.assertEqual(deps.resolve(graph, "a"), ["c", "b", "a"])

    def test_deep_chain_with_shared_tail_is_fast(self):
        graph = {f"n{i}": [f"n{i + 1}", f"n{i + 2}"] for i in range(40)}
        self.assertEqual(len(deps.resolve(graph, "n0")), 42)
