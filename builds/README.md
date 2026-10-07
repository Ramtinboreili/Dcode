# Builds

Ready-made binaries, so a new machine can run Dcode without building it first.

| File | Platform | Built from | Reports version |
|---|---|---|---|
| `macos-arm64/dcode` | macOS, Apple Silicon | commit `0456097` on 2026-10-07 (0.3.1 plus the unreleased Phase 3 work) | 0.3.1 |

SHA-256 of `macos-arm64/dcode`: `fb577199db5ab6e731de7f2fc893fb6f2dc10930c6726ac6d06ba239b835c4d8`

Install on an Apple Silicon Mac (no sudo needed):

```bash
install -m 0755 builds/macos-arm64/dcode /opt/homebrew/bin/dcode
dcode --version
```

It passed all 31 checks of `docs/dev/terminal_e2e.py --binary`.

There is **no Linux build yet**. A `.deb` has to be built on a matching Linux machine with `make deb` (see `docs/operations.md`).

To rebuild: `make mac PYTHON=.venv/bin/python`, then copy `dist/dcode` here.
