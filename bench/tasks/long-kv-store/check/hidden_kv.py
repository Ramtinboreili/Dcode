import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


from kv import KV, Closed

TRICKY = {"a\nb": "c\td", "ключ": "значение 🎉", "with space": "", "quote\"'": "\\back\\"}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.wal = Path(self.dir, "wal.log")


class Basics(Base):
    def test_put_get_delete(self):
        with KV(self.dir) as db:
            db.put("a", "1")
            self.assertEqual(db.get("a"), "1")
            self.assertIsNone(db.get("b"))
            self.assertEqual(db.get("b", "x"), "x")
            db.delete("a")
            db.delete("never")
            self.assertIsNone(db.get("a"))

    def test_persist_and_order(self):
        with KV(self.dir) as db:
            for key in ("c", "a", "b"):
                db.put(key, key.upper())
            db.put("a", "again")
            db.delete("b")
        with KV(self.dir) as db:
            self.assertEqual(db.keys(), ["a", "c"])
            self.assertEqual(db.items(), [("a", "again"), ("c", "C")])

    def test_tricky_text(self):
        with KV(self.dir) as db:
            for key, value in TRICKY.items():
                db.put(key, value)
        with KV(self.dir) as db:
            self.assertEqual(dict(db.items()), TRICKY)

    def test_wal_written_before_return(self):
        db = KV(self.dir)
        db.put("a", "1")
        size = self.wal.stat().st_size
        self.assertGreater(size, 0)
        copy = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, copy)
        shutil.copy(self.wal, copy)
        with KV(copy) as other:
            self.assertEqual(other.get("a"), "1")
        db.close()

    def test_closed(self):
        db = KV(self.dir)
        db.close()
        db.close()
        for call in (lambda: db.get("a"), lambda: db.put("a", "1"), lambda: db.keys(), lambda: db.compact()):
            with self.assertRaises(Closed):
                call()


class Crash(Base):
    def fill(self):
        with KV(self.dir) as db:
            db.put("a", "1")
            db.put("b", "2")
            db.put("c", "3")

    def test_torn_tail(self):
        self.fill()
        whole = self.wal.read_bytes()
        for cut in (2, 3, 5):  # 1 byte may be only a final newline, which a format can tolerate
            self.wal.write_bytes(whole[:-cut])
            with KV(self.dir) as db:
                self.assertEqual(db.items(), [("a", "1"), ("b", "2")], f"cut {cut}")
            self.wal.write_bytes(whole)

    def test_writes_after_a_torn_tail_survive(self):
        self.fill()
        self.wal.write_bytes(self.wal.read_bytes()[:-3])
        with KV(self.dir) as db:
            db.put("d", "4")
        with KV(self.dir) as db:
            self.assertEqual(db.items(), [("a", "1"), ("b", "2"), ("d", "4")])

    def test_garbage_tail(self):
        self.fill()
        with open(self.wal, "ab") as handle:
            handle.write(b"\x00\xff garbage \x80\n more")
        with KV(self.dir) as db:
            self.assertEqual(len(db.keys()), 3)
            db.put("d", "4")
        with KV(self.dir) as db:
            self.assertEqual(len(db.keys()), 4)

    def test_flipped_byte_in_last_record(self):
        self.fill()
        raw = bytearray(self.wal.read_bytes())
        raw[-4] ^= 0x01
        self.wal.write_bytes(raw)
        with KV(self.dir) as db:
            self.assertEqual(db.keys(), ["a", "b"])

    def test_flipped_byte_stops_replay_there(self):
        self.fill()
        raw = bytearray(self.wal.read_bytes())
        raw[len(raw) // 2] ^= 0x01
        self.wal.write_bytes(raw)
        with KV(self.dir) as db:
            self.assertNotIn("c", db.keys())


class Batches(Base):
    def test_batch(self):
        with KV(self.dir) as db:
            db.put("y", "old")
            with db.batch() as b:
                b.put("x", "1")
                b.delete("y")
            self.assertEqual(db.items(), [("x", "1")])
        with KV(self.dir) as db:
            self.assertEqual(db.items(), [("x", "1")])

    def test_exception_discards(self):
        with KV(self.dir) as db:
            db.put("keep", "1")
            with self.assertRaises(ZeroDivisionError):
                with db.batch() as b:
                    b.put("x", "1")
                    1 / 0
            self.assertEqual(db.keys(), ["keep"])
        with KV(self.dir) as db:
            self.assertEqual(db.keys(), ["keep"])

    def test_torn_batch_is_all_or_nothing(self):
        with KV(self.dir) as db:
            db.put("first", "1")
            before = self.wal.stat().st_size
            with db.batch() as b:
                for i in range(20):
                    b.put(f"k{i}", "v" * 20)
        whole = self.wal.read_bytes()
        added = len(whole) - before
        for cut in (2, added // 4, added // 2, added - 1):
            self.wal.write_bytes(whole[:-cut])
            with KV(self.dir) as db:
                self.assertEqual(db.keys(), ["first"], f"cut {cut}")
            self.wal.write_bytes(whole)
        with KV(self.dir) as db:
            self.assertEqual(len(db.keys()), 21)


class Compaction(Base):
    def test_shrinks_and_keeps(self):
        with KV(self.dir) as db:
            for i in range(300):
                db.put("key", str(i) * 10)
            db.put("other", "x")
            db.delete("other")
            before = db.disk_size()
            db.compact()
            self.assertLess(db.disk_size(), before / 5)
            self.assertEqual(db.items(), [("key", "299" * 10)])
            db.put("new", "1")
        with KV(self.dir) as db:
            self.assertEqual(db.items(), [("key", "299" * 10), ("new", "1")])
            self.assertEqual(self.wal.exists(), True)

    def test_tricky_text_survives(self):
        with KV(self.dir) as db:
            for key, value in TRICKY.items():
                db.put(key, value)
            db.compact()
        with KV(self.dir) as db:
            self.assertEqual(dict(db.items()), TRICKY)

    def test_leftover_temp_is_ignored(self):
        with KV(self.dir) as db:
            db.put("a", "1")
            db.compact()
            names = set(os.listdir(self.dir))
        for name in names - {"wal.log"}:
            Path(self.dir, name + ".tmp").write_bytes(b"half written")
        Path(self.dir, "snapshot.tmp").write_bytes(b"half written")
        with KV(self.dir) as db:
            self.assertEqual(db.items(), [("a", "1")])

    def test_crash_between_rename_and_truncate(self):
        with KV(self.dir) as db:
            db.put("a", "1")
            db.put("gone", "2")
            db.delete("gone")
            db.put("a", "3")
            old_log = self.wal.read_bytes()
            db.compact()
            db.close()
        self.wal.write_bytes(old_log)  # the log survived the crash whole
        with KV(self.dir) as db:
            self.assertEqual(db.items(), [("a", "3")])
            db.delete("a")
        with KV(self.dir) as db:
            self.assertEqual(db.items(), [])


class Cli(Base):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "kv", self.dir, *args], capture_output=True, text=True)

    def test_commands(self):
        self.assertEqual(self.run_cli("put", "a", "1").returncode, 0)
        self.assertEqual(self.run_cli("put", "b", "2").returncode, 0)
        got = self.run_cli("get", "a")
        self.assertEqual((got.returncode, got.stdout.strip()), (0, "1"))
        self.assertEqual(self.run_cli("get", "zz").returncode, 1)
        self.assertEqual(self.run_cli("keys").stdout.split(), ["a", "b"])
        self.assertEqual(self.run_cli("del", "a").returncode, 0)
        self.assertEqual(self.run_cli("compact").returncode, 0)
        self.assertEqual(self.run_cli("keys").stdout.split(), ["b"])
