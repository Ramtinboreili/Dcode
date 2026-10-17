# Builds

Ready-made binaries, so a new machine can run Dcode without building it first.

| File | Platform | Built from | Reports version |
|---|---|---|---|
| `macos-arm64/dcode` | macOS, Apple Silicon | commit `0456097` on 2026-10-07 (0.3.1 plus the unreleased Phase 3 work) | 0.3.1 |
| `linux-amd64/` (`.deb`, `.rpm`, `.tar.gz`, `.vsix`) | Linux x86_64, any distribution with glibc 2.28 or newer (built on AlmaLinux 8 with `make packages`) | the `Release 0.7.1` commit, 2026-10-17 | 0.7.1 |

SHA-256 of `macos-arm64/dcode`: `fb577199db5ab6e731de7f2fc893fb6f2dc10930c6726ac6d06ba239b835c4d8`

Install on an Apple Silicon Mac (no sudo needed):

```bash
install -m 0755 builds/macos-arm64/dcode /opt/homebrew/bin/dcode
dcode --version
```

It passed all 31 checks of `docs/dev/terminal_e2e.py --binary`.

## Linux (x86_64), 0.7.1

SHA-256:

```
6aaf21bbb39b5c241736eb299a10fc44d17cddea7cb0849fcf84268663986002  dcode-0.7.1-1.x86_64.rpm
f9bb344b7271347c6c0b1027001499324b0db2acf02829ebdb1c065443fc8648  dcode_0.7.1_amd64.deb
844a5d96119f30a04835954266fa441add7ed7004d007d4dc8591aa69e90f434  dcode-0.7.1-linux-x86_64.tar.gz
efd92a2407c65ffe02ea497c420862882dbfebcd907e513d149b55f5ad1a9059  dcode-0.7.1.vsix
```

```bash
sudo packaging/install-airgap.sh builds/linux-amd64/dcode_0.7.1_amd64.deb       # Debian / Ubuntu
sudo packaging/install-airgap.sh builds/linux-amd64/dcode-0.7.1-1.x86_64.rpm    # RHEL / Rocky / Alma / Fedora / SUSE
tar -xzf builds/linux-amd64/dcode-0.7.1-linux-x86_64.tar.gz && ./dcode-0.7.1-linux-x86_64/install.sh   # any Linux, no root
code --install-extension builds/linux-amd64/dcode-0.7.1.vsix                    # the VS Code extension (any OS)
dcode --version
```

- All three packages hold the same binary. Python and every dependency are inside, including the PostgreSQL and MySQL drivers.
- It was built on glibc 2.28, so it runs on RHEL 8+, Debian 10+ and Ubuntu 20.04+. The 0.7.1 packages were installed and run in AlmaLinux 8 and Debian 11 containers (0.5.0 also in AlmaLinux 9 and Ubuntu 20.04).
- The binary passed all 64 checks of `docs/dev/terminal_e2e.py --binary`.
- The Docker image is not stored here (85 MB): `make docker` builds `dist/dcode-0.7.1-docker-x86_64.tar.gz`.
- The 0.4.0 `.deb` was removed: it needed glibc 2.35 and Debian 11 could not open it.
- The macOS binary above is still 0.3.1; rebuild it on a Mac. There is no arm64 Linux build.

To rebuild: `make mac PYTHON=.venv/bin/python`, then copy `dist/dcode` here.
