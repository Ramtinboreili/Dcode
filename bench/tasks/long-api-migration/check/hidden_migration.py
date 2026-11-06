import ast
import importlib
import json
import unittest
from pathlib import Path
from unittest import mock

import httpkit

TABLE = {"accounts": [True, 0, 10], "alerts": [False, 0, 10], "audit": [True, 3, 10], "billing": [False, 0, 5], "cart": [True, 0, 10], "catalog": [False, 2, 30], "checkout": [True, 0, 10], "coupons": [False, 0, 10], "customers": [True, 3, 10], "dashboards": [False, 0, 5], "deliveries": [True, 0, 10], "devices": [False, 2, 30], "discounts": [True, 0, 10], "events": [False, 0, 10], "exports": [True, 3, 10], "feeds": [False, 0, 5], "files": [True, 0, 10], "gifts": [False, 2, 30], "groups": [True, 0, 10], "history": [False, 0, 10], "inventory": [True, 3, 10], "invoices": [False, 0, 5], "jobs": [True, 0, 10], "labels": [False, 2, 30], "leads": [True, 0, 10], "metrics": [False, 0, 10], "notes": [True, 3, 10], "offers": [False, 0, 5], "orders": [True, 0, 10], "partners": [False, 2, 30], "payments": [True, 0, 10], "plans": [False, 0, 10], "quotes": [True, 3, 10], "refunds": [False, 0, 5], "reports": [True, 0, 10], "returns": [False, 2, 30], "reviews": [True, 0, 10], "sessions": [False, 0, 10], "stock": [True, 3, 10], "tickets": [False, 0, 5]}


class Hidden(unittest.TestCase):
    def test_behaviour_is_the_same(self):
        for name, (as_json, retries, timeout) in TABLE.items():
            with self.subTest(name), mock.patch.object(httpkit, "_send", return_value=json.dumps({"n": name})) as send:
                got = importlib.import_module("services." + name).load()
                self.assertEqual(got, {"n": name} if as_json else json.dumps({"n": name}))
                self.assertEqual(send.call_args.args, ("GET", "https://api.internal/" + name, timeout, retries))

    def test_fetch_is_gone(self):
        self.assertFalse(hasattr(httpkit, "fetch"))
        for path in [*Path("services").glob("*.py"), Path("httpkit.py")]:
            for node in ast.walk(ast.parse(path.read_text())):
                self.assertFalse(isinstance(node, ast.Name) and node.id == "fetch", str(path))
                self.assertFalse(isinstance(node, ast.Attribute) and node.attr == "fetch", str(path))
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn("fetch", [a.name for a in node.names], str(path))
