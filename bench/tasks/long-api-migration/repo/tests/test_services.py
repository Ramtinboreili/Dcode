import importlib
import json
import unittest
from pathlib import Path
from unittest import mock

import httpkit


class Test(unittest.TestCase):
    def test_all_load(self):
        with mock.patch.object(httpkit, "_send", return_value=json.dumps({"ok": 1})):
            for path in Path("services").glob("[a-z]*.py"):
                importlib.import_module("services." + path.stem).load()
