#!/usr/bin/env python3
"""Build docs/presentation.html: inline the screenshots into the template.

The result is one self-contained file (works offline, easy to share or present
on an air-gapped machine). Edit template.html or replace screens/*.jpg, then run:

    python docs/presentation/build.py

Refresh screenshots with docs/dev/screen.py (terminal) and headless Chrome
(web UI) — see docs/testing.md.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template.html"
OUTPUT = HERE.parent / "presentation.html"


def main() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")

    def inline(match: re.Match) -> str:
        image = HERE / "screens" / f"{match.group(1)}.jpg"
        return "data:image/jpeg;base64," + base64.b64encode(image.read_bytes()).decode("ascii")

    html, count = re.subn(r"\{\{IMG:([\w-]+)\}\}", inline, html)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(HERE.parent.parent)} ({count} images, {OUTPUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
