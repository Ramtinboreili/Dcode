---
name: macos-editable-install-hidden-pth
description: "On this Mac the repo's .venv editable install breaks — .pth files get the macOS \"hidden\" flag and Python 3.12.4 skips them"
metadata:
  node_type: memory
  type: reference
  originSessionId: 8ea51d4c-aca8-475a-be52-47f40c3dac22
  modified: 2026-10-03T19:42:40.797Z
---

The repo lives under ~/Documents (appears synced); files created in `.venv/lib/python3.12/site-packages/__editable__*.pth` get the `hidden` flag (re-applied even after `chflags nohidden`). Python 3.12.4 ignores hidden .pth files, so `import dcode` only works from the repo root.

Workaround when running `dcode` from elsewhere (e.g. e2e tests): set `PYTHONPATH=<repo>`. Permanent fix options: venv outside ~/Documents, or non-editable `pip install .`.
