# kv

A small persistent key-value store for string keys and values. Nothing exists yet, only this file.

    from kv import KV
    with KV("data/") as db:
        db.put("a", "1")
        db.get("a")            # "1"
        db.get("b", "x")       # default, None if not given
        db.delete("a")
        db.keys()              # sorted list
        db.items()             # sorted list of (key, value)
        with db.batch() as b:  # all or nothing, also across a crash; an exception inside discards it
            b.put("x", "1")
            b.delete("y")
        db.compact()           # rewrite so the disk only holds live data
        db.disk_size()         # total bytes of the store's files

Rules

- `<dir>/wal.log` is the write-ahead log. Every put/delete/batch is appended and flushed to the OS before the call returns.
- A crash can leave a torn or corrupt record at the end of the log. Opening the store must recover everything before that record and drop the rest, and new writes must still work after that. Records need a checksum (zlib.crc32 is fine).
- `compact()` writes a snapshot to a temp file and renames it into place, then empties the log. A leftover temp file from a crash is ignored and removed at open. A crash between the rename and emptying the log must not lose or resurrect anything.
- Keys and values can hold any text: newlines, tabs, unicode.
- After `close()`, anything but `close()` raises `kv.Closed`. `get` on a store never raises for a missing key; `delete` of a missing key is fine.

CLI: `python -m kv DIR put K V`, `get K` (prints the value, exit 1 if missing), `del K`, `keys` (one per line), `compact`.
