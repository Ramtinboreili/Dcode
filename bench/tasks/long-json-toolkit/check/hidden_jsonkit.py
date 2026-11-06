import ast
import copy
import json
import random
import unittest
from pathlib import Path

from jsonkit import JsonError, dumps, loads
from jsonkit.patch import PatchError, apply_patch, diff
from jsonkit.pointer import PointerError, resolve


class Parse(unittest.TestCase):
    def test_values(self):
        for text in ['1', '-0', '0.5', '1e3', '-1.5E-2', 'true', 'false', 'null', '""', '"a"', '[]', '{}', ' [1, 2 , {"a" : [] } ] ', '{"a":1,"a":2}',
                     '"\\u00e9\\n\\t\\"\\\\\\/"', '"\\ud83d\\ude00"', '"😀 é"', '[[[[]]]]', '123456789012345678901234567890']:
            with self.subTest(text):
                want = json.loads(text)
                got = loads(text)
                self.assertEqual(got, want)
                self.assertEqual(type(got), type(want))

    def test_rejects(self):
        for text in ['', ' ', '01', '-', '1.', '.5', '+1', '1e', '[1,]', '{"a":1,}', '{a:1}', "{'a':1}", '[1 2]', '{"a" 1}', 'nul', 'True', 'NaN', 'Infinity',
                     '"a', '"\\x"', '"\\u12"', '"\t"', '"\n"', '"\\ud83d"', '"\\ude00"', '"\\ud83d\\u0041"', '// c\n1', '/* c */1', '1 2', '[', '{', '{"a"', '{"a":', '[1,', '"\\']:
            with self.subTest(text):
                with self.assertRaises(JsonError):
                    loads(text)

    def test_position(self):
        with self.assertRaises(JsonError) as caught:
            loads('{\n  "a": 1,\n  "b": tru\n}')
        error = caught.exception
        self.assertEqual(error.line, 3)
        self.assertIn(error.col, range(8, 12))  # the start of the bad word, or where it stops making sense
        self.assertEqual(error.pos, 11 + error.col)
        with self.assertRaises(JsonError) as caught:
            loads('[1,\n2,]')
        self.assertEqual((caught.exception.line, caught.exception.col), (2, 3))

    def test_deep(self):
        text = "[" * 10000 + "]" * 10000
        try:
            loads(text)
        except JsonError:
            pass
        text = '{"a":' * 10000 + "1" + "}" * 10000
        try:
            loads(text)
        except JsonError:
            pass


class Write(unittest.TestCase):
    def test_compact(self):
        self.assertEqual(dumps({"a": 1, "b": [1, 2]}), '{"a":1,"b":[1,2]}')
        self.assertEqual(dumps([]), "[]")
        self.assertEqual(dumps({"z": {}, "y": []}), '{"z":{},"y":[]}')
        self.assertEqual(dumps({"b": 1, "a": 2}, sort_keys=True), '{"a":2,"b":1}')
        self.assertEqual(dumps(True), "true")
        self.assertEqual(dumps(None), "null")
        self.assertEqual(dumps(1.5), "1.5")

    def test_indent(self):
        value = {"a": 1, "b": [1, {"c": []}], "d": {}}
        self.assertEqual(dumps(value, indent=2), json.dumps(value, indent=2))
        self.assertEqual(dumps(value, indent=4), json.dumps(value, indent=4))

    def test_strings(self):
        for text in ['"\\', "\n\t\r\b\f", "\x01\x1f", "é", "😀", "a/b", "\u2028"]:
            with self.subTest(text):
                self.assertEqual(json.loads(dumps(text)), text)
                self.assertEqual(loads(dumps(text)), text)
                escaped = dumps(text, ascii_only=True)
                self.assertTrue(escaped.isascii())
                self.assertEqual(loads(escaped), text)

    def test_rejects(self):
        for bad in [b"x", {1}, object()]:
            with self.assertRaises(TypeError):
                dumps(bad)
        with self.assertRaises(TypeError):
            dumps({1: "a"})
        for bad in [float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                dumps(bad)

    def test_round_trip_random(self):
        rng = random.Random(7)

        def make(depth):
            kind = rng.randrange(7 if depth < 4 else 5)
            if kind == 0:
                return rng.randint(-10 ** 12, 10 ** 12)
            if kind == 1:
                return rng.uniform(-1e6, 1e6)
            if kind == 2:
                return rng.choice([None, True, False])
            if kind in (3, 4):
                return "".join(rng.choice('ab"\\/\n\té😀 ') for _ in range(rng.randrange(8)))
            if kind == 5:
                return [make(depth + 1) for _ in range(rng.randrange(4))]
            return {str(rng.randrange(50)): make(depth + 1) for _ in range(rng.randrange(4))}

        for _ in range(300):
            value = make(0)
            self.assertEqual(loads(dumps(value)), value)
            self.assertEqual(json.loads(dumps(value)), value)
            self.assertEqual(loads(json.dumps(value)), value)


class Pointer(unittest.TestCase):
    DOC = {"foo": ["bar", "baz"], "": 0, "a/b": 1, "c%d": 2, "e^f": 3, "g|h": 4, "i\\j": 5, 'k"l': 6, " ": 7, "m~n": 8}

    def test_rfc_examples(self):
        for pointer, want in [("", self.DOC), ("/foo", ["bar", "baz"]), ("/foo/0", "bar"), ("/", 0), ("/a~1b", 1), ("/c%d", 2), ("/e^f", 3), ("/g|h", 4), ("/i\\j", 5), ('/k"l', 6), ("/ ", 7), ("/m~0n", 8)]:
            with self.subTest(pointer):
                self.assertEqual(resolve(self.DOC, pointer), want)

    def test_order_of_unescape(self):
        self.assertEqual(resolve({"~1": "right", "/": "wrong"}, "/~01"), "right")

    def test_errors(self):
        for pointer in ["foo", "/nope", "/foo/2", "/foo/-", "/foo/01", "/foo/-1", "/foo/x", "/foo/0/z", "/m~2n", "/m~"]:
            with self.subTest(pointer), self.assertRaises(PointerError):
                resolve(self.DOC, pointer)


class Patch(unittest.TestCase):
    CASES = [
        ({"foo": "bar"}, [{"op": "add", "path": "/baz", "value": "qux"}], {"baz": "qux", "foo": "bar"}),
        ({"foo": ["bar", "baz"]}, [{"op": "add", "path": "/foo/1", "value": "qux"}], {"foo": ["bar", "qux", "baz"]}),
        ({"baz": "qux", "foo": "bar"}, [{"op": "remove", "path": "/baz"}], {"foo": "bar"}),
        ({"foo": ["bar", "qux", "baz"]}, [{"op": "remove", "path": "/foo/1"}], {"foo": ["bar", "baz"]}),
        ({"baz": "qux", "foo": "bar"}, [{"op": "replace", "path": "/baz", "value": "boo"}], {"baz": "boo", "foo": "bar"}),
        ({"foo": {"bar": "baz", "waldo": "fred"}, "qux": {"corge": "grault"}}, [{"op": "move", "from": "/foo/waldo", "path": "/qux/thud"}], {"foo": {"bar": "baz"}, "qux": {"corge": "grault", "thud": "fred"}}),
        ({"foo": ["all", "grass", "cows", "eat"]}, [{"op": "move", "from": "/foo/1", "path": "/foo/3"}], {"foo": ["all", "cows", "eat", "grass"]}),
        ({"foo": ["bar"]}, [{"op": "add", "path": "/foo/-", "value": ["abc", "def"]}], {"foo": ["bar", ["abc", "def"]]}),
        ({"foo": "bar"}, [{"op": "add", "path": "", "value": [1]}], [1]),
        ({"foo": 1}, [{"op": "replace", "path": "", "value": 2}], 2),
        ({"a": {"b": 1}}, [{"op": "copy", "from": "/a", "path": "/c"}], {"a": {"b": 1}, "c": {"b": 1}}),
        ({"a": [1, 2]}, [{"op": "test", "path": "/a", "value": [1.0, 2]}], {"a": [1, 2]}),
        ({"a/b": 1}, [{"op": "move", "from": "/a~1b", "path": "/c~0d"}], {"c~d": 1}),
        ({"a": 1}, [{"op": "move", "from": "/a", "path": "/a"}], {"a": 1}),
    ]
    BAD = [
        ({"a": 1}, [{"op": "remove", "path": "/b"}]),
        ({"a": 1}, [{"op": "replace", "path": "/b", "value": 1}]),
        ({"a": 1}, [{"op": "add", "path": "/b/c", "value": 1}]),
        ({"a": [1]}, [{"op": "add", "path": "/a/5", "value": 1}]),
        ({"a": [1]}, [{"op": "remove", "path": "/a/1"}]),
        ({"a": 1}, [{"op": "test", "path": "/a", "value": 2}]),
        ({"a": 1}, [{"op": "test", "path": "/a", "value": True}]),
        ({"a": 1}, [{"op": "move", "from": "/x", "path": "/y"}]),
        ({"a": {"b": 1}}, [{"op": "move", "from": "/a", "path": "/a/b/c"}]),
        ({"a": 1}, [{"op": "copy", "from": "/x", "path": "/y"}]),
        ({"a": 1}, [{"op": "nope", "path": "/a"}]),
        ({"a": 1}, [{"path": "/a"}]),
        ({"a": 1}, [{"op": "add", "path": "/b"}]),
    ]

    def test_cases(self):
        for doc, ops, want in self.CASES:
            with self.subTest(ops):
                before = copy.deepcopy(doc)
                self.assertEqual(apply_patch(doc, ops), want)
                self.assertEqual(doc, before)

    def test_errors(self):
        for doc, ops in self.BAD:
            with self.subTest(ops), self.assertRaises(PatchError):
                apply_patch(doc, ops)

    def test_all_or_nothing(self):
        doc = {"a": 1}
        with self.assertRaises(PatchError):
            apply_patch(doc, [{"op": "add", "path": "/b", "value": 2}, {"op": "remove", "path": "/zz"}])
        self.assertEqual(doc, {"a": 1})

    def test_values_are_copied(self):
        value = {"x": []}
        got = apply_patch({}, [{"op": "add", "path": "/a", "value": value}])
        got["a"]["x"].append(1)
        self.assertEqual(value, {"x": []})

    def test_diff(self):
        rng = random.Random(11)

        def make(depth):
            kind = rng.randrange(6 if depth < 4 else 4)
            if kind < 3:
                return rng.choice([0, 1, True, False, None, "s", "t", 2.5])
            if kind == 3:
                return rng.choice(["x", 9])
            if kind == 4:
                return [make(depth + 1) for _ in range(rng.randrange(4))]
            return {rng.choice(["a", "b", "c/d", "e~f", ""]): make(depth + 1) for _ in range(rng.randrange(4))}

        for _ in range(400):
            a, b = make(0), make(0)
            self.assertEqual(diff(a, a), [])
            got = apply_patch(a, diff(a, b))
            self.assertEqual(json.dumps(got, sort_keys=True), json.dumps(b, sort_keys=True))


class Rules(unittest.TestCase):
    def test_no_json_module_or_eval(self):
        files = list(Path("jsonkit").glob("*.py"))
        self.assertTrue(files)
        for path in files:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    self.assertNotIn("json", [a.name.split(".")[0] for a in node.names], str(path))
                if isinstance(node, ast.ImportFrom) and not node.level:
                    self.assertNotEqual((node.module or "").split(".")[0], "json", str(path))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, ("eval", "exec"), str(path))
