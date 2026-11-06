import ast
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GOLDEN = {'all': [0, '== high_margin\nHIGH MARGIN (5)\nD400     Robot                    7 W3       46.80\nA100     Hammer                  12 W1       16.80\nC300     Novel                   40 W1       12.00\nD401     Kite                   120 W2        6.60\nB200     Apples                 200 W2        0.68\n== low_stock\nLOW STOCK (3)\nB201     Rice                     0 W2        1.99\nC301     Atlas                    2 W3       25.00\nA101     Saw                      3 W1       13.20\n== margin_by_category\nCATEGORY         MARGIN\nbooks            326.00\nfood              90.00\ngarden            40.00\nmisc               5.00\ntools             96.00\ntoys             588.00\nTOTAL           1145.00\n== margin_by_warehouse\nWAREHOUSE        MARGIN\nW1               421.00\nW2               510.00\nW3               214.00\nTOTAL           1145.00\n== out_of_stock\nOUT OF STOCK (1)\nB201     Rice                     0 W2        1.99\n== overstock\nOVERSTOCK (2)\nD401     Kite                   120 W2        6.60\nB200     Apples                 200 W2        0.68\n== qty_by_category\nCATEGORY            QTY\nbooks             42.00\nfood             200.00\ngarden            16.00\nmisc               5.00\ntools             15.00\ntoys             127.00\nTOTAL            405.00\n== qty_by_warehouse\nWAREHOUSE           QTY\nW1                60.00\nW2               320.00\nW3                25.00\nTOTAL            405.00\n== reprice\nREPRICE (2)\nC301     Atlas                    2 W3       25.00\nA101     Saw                      3 W1       13.20\n== retail_by_category\nCATEGORY         RETAIL\nbooks            530.00\nfood             130.00\ngarden           168.00\nmisc              10.00\ntools            201.00\ntoys             933.00\nTOTAL           1972.00\n== retail_by_warehouse\nWAREHOUSE        RETAIL\nW1               691.00\nW2               790.00\nW3               491.00\nTOTAL           1972.00\n== top_margin\nTOP MARGIN\n1. D400     Robot                     24.00\n2. C300     Novel                      8.00\n3. A100     Hammer                     7.50\n== top_qty\nTOP QTY\n1. B200     Apples                   200.00\n2. D401     Kite                     120.00\n3. C300     Novel                     40.00\n== top_retail\nTOP RETAIL\n1. D401     Kite                     660.00\n2. C300     Novel                    480.00\n3. D400     Robot                    273.00\n== top_value\nTOP VALUE\n1. D401     Kite                     240.00\n2. C300     Novel                    160.00\n3. E500     Spade                    128.00\n== untaxed\nUNTAXED (2)\nC301     Atlas                    2 W3       25.00\nC300     Novel                   40 W1       12.00\n== value_by_category\nCATEGORY          VALUE\nbooks            204.00\nfood              40.00\ngarden           128.00\nmisc               5.00\ntools            105.00\ntoys             345.00\nTOTAL            827.00\n== value_by_warehouse\nWAREHOUSE         VALUE\nW1               270.00\nW2               280.00\nW3               277.00\nTOTAL            827.00\n(loaded 1 times)\n', ''], 'summary': [0, 'W1: 60 units, 270.00, books, misc, tools\nW2: 320 units, 280.00, food, toys\nW3: 25 units, 277.00, books, garden, toys\n', ''], 'quote D401 50': [0, '297.00\n', ''], 'quote A101 9': [3, '', 'only 3 of A101\n'], 'quote nope 1': [2, '', 'no such sku nope\n'], 'report nope': [2, '', 'unknown report nope\n'], 'report reprice': [0, 'REPRICE (2)\nC301     Atlas                    2 W3       25.00\nA101     Saw                      3 W1       13.20\n', '']}
DATA = 'sku,name,category,qty,cost,price,warehouse\nA100,Hammer,tools,12,6.50,14.00,W1\nA101,Saw,tools,3,9.00,11.00,W1\nB200,Apples,food,200,0.20,0.65,W2\nB201,Rice,food,0,1.10,1.90,W2\nC300,Novel,books,40,4.00,12.00,W1\nC301,Atlas,books,2,22.00,25.00,W3\nD400,Robot,toys,7,15.00,39.00,W3\nD401,Kite,toys,120,2.00,5.50,W2\nE500,Spade,garden,16,8.00,10.50,W3\n# a comment row\nF600,Mystery,misc,5,1.00,2.00,W1\n'
WANT_LOCATIONS = {
    "models": ["Item", "Warehouse", "ValidationError"],
    "storage": ["load_items", "save_items", "parse_row", "build_warehouses"],
    "pricing": ["tax_rate", "price_with_tax", "volume_discount", "order_total", "markup"],
    "reports": ["REPORTS", "report_low_stock", "warehouse_summary"],
    "cli": ["main", "cmd_report", "cmd_quote"],
}
ROOT = Path.cwd()


def run(*args, cwd):
    return subprocess.run([sys.executable, str(ROOT / "legacy_inventory.py"), "stock.csv", *args], cwd=cwd, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})


class Hidden(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        Path(self.tmp, "stock.csv").write_text(DATA)
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_output_is_byte_identical(self):
        for command, want in GOLDEN.items():
            with self.subTest(command):
                got = run(*command.split(), cwd=self.tmp)
                self.assertEqual([got.returncode, got.stdout, got.stderr], want)

    def test_restock_writes_the_file(self):
        self.assertEqual(run("restock", "A101", "4", cwd=self.tmp).returncode, 0)
        self.assertIn("A101,Saw,tools,7,9.00,11.00,W1", Path(self.tmp, "stock.csv").read_text())

    def test_the_layout(self):
        for module, names in WANT_LOCATIONS.items():
            path = Path(f"inventory/{module}.py")
            self.assertTrue(path.is_file(), str(path))
            tree = ast.parse(path.read_text())
            found = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
            found |= {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
            found |= {n.target.id for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
            for name in names:
                self.assertIn(name, found, f"{name} belongs in {module}")

    def test_the_old_script_is_a_thin_shim(self):
        self.assertLess(len(Path("legacy_inventory.py").read_text().splitlines()), 25)

    def test_no_module_is_a_monolith(self):
        for path in Path("inventory").glob("*.py"):
            self.assertLess(len(path.read_text().splitlines()), 420, str(path))

    def test_each_module_imports_alone(self):
        for module in WANT_LOCATIONS:
            done = subprocess.run([sys.executable, "-c", f"import inventory.{module}"], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_models_import_nothing_from_the_package(self):
        tree = ast.parse(Path("inventory/models.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("inventory") or node.level, "models stays at the bottom")
