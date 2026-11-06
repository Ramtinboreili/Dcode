#!/usr/bin/env python3
"""Show the biggest files under a folder."""
import argparse
import os
import sys


def human(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return ""


def scan(root):
    rows = []
    for folder, _, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            rows.append((os.path.relpath(path, root), os.path.getsize(path)))
    rows.sort(key=lambda row: (-row[1], row[0]))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"not a folder: {args.root}", file=sys.stderr)
        return 2
    for path, size in scan(args.root)[:args.top]:
        print(f"{human(size):>10}  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
