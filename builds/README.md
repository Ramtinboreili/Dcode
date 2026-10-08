# Builds

Ready-made binaries, so a new machine can run Dcode without building it first.

| File | Platform | Built from | Reports version |
|---|---|---|---|
| `macos-arm64/dcode` | macOS, Apple Silicon | commit `0456097` on 2026-10-07 (0.3.1 plus the unreleased Phase 3 work) | 0.3.1 |
| `linux-amd64/dcode_0.4.0_amd64.deb` | Linux x86_64 (built on Ubuntu 22.04, glibc 2.35) | the `Release 0.4.0` commit, 2026-10-08 | 0.4.0 |

SHA-256 of `macos-arm64/dcode`: `fb577199db5ab6e731de7f2fc893fb6f2dc10930c6726ac6d06ba239b835c4d8`

Install on an Apple Silicon Mac (no sudo needed):

```bash
install -m 0755 builds/macos-arm64/dcode /opt/homebrew/bin/dcode
dcode --version
```

It passed all 31 checks of `docs/dev/terminal_e2e.py --binary`.

## Linux (x86_64)

SHA-256 of `linux-amd64/dcode_0.4.0_amd64.deb`: `7ba982e304b64b81f4bce655e049b0b9d9760699560bfd783698cfc0b6fffcf2`

```bash
sudo packaging/install-airgap.sh builds/linux-amd64/dcode_0.4.0_amd64.deb   # Debian / Ubuntu (or: sudo dpkg -i …)
dcode --version

# Any other Linux, or without root: take the binary out of the package
dpkg-deb -x builds/linux-amd64/dcode_0.4.0_amd64.deb /tmp/dcode-pkg && install -m 0755 /tmp/dcode-pkg/usr/bin/dcode ~/.local/bin/dcode
```

- Python and every dependency are inside, including the PostgreSQL and MySQL drivers. Nothing else is needed on the server.
- It was built on Ubuntu 22.04, so it needs **glibc 2.35 or newer**: Ubuntu 22.04+, Debian 12+, Fedora 36+. It does **not** run on RHEL / Rocky / Alma 8 or 9 (glibc 2.28 and 2.34), Debian 11 or Ubuntu 20.04. (An earlier version of this note wrongly listed RHEL 9.2+.)
- The package is zstd-compressed, which `dpkg` on Debian 11 and older can't open.
- Both limits are gone in builds made with `make packages` (see `docs/operations.md`): the binary is built on glibc 2.28 and the `.deb` uses xz. The next release should be built that way; this file is kept as the 0.4.0 that was published.
- The binary from this package passed all 42 checks of `docs/dev/terminal_e2e.py --binary`.
- The macOS binary above is still 0.3.1; rebuild it on a Mac for 0.4.0.

For another CPU (arm64) build on a machine of that kind: `make deb PYTHON=.venv/bin/python` (see `docs/operations.md`).

To rebuild: `make mac PYTHON=.venv/bin/python`, then copy `dist/dcode` here.
