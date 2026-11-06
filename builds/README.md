# Builds

Ready-made binaries, so a new machine can run Dcode without building it first.

| File | Platform | Built from | Reports version |
|---|---|---|---|
| `macos-arm64/dcode` | macOS, Apple Silicon | commit `0456097` on 2026-10-07 (0.3.1 plus the unreleased Phase 3 work) | 0.3.1 |
| `linux-amd64/` (`.deb`, `.rpm`, `.tar.gz`, `.vsix`) | Linux x86_64, any distribution with glibc 2.28 or newer (built on AlmaLinux 8 with `make packages`) | the `Release 0.8.0` commit, 2026-11-06 | 0.8.0 |

SHA-256 of `macos-arm64/dcode`: `fb577199db5ab6e731de7f2fc893fb6f2dc10930c6726ac6d06ba239b835c4d8`

Install on an Apple Silicon Mac (no sudo needed):

```bash
install -m 0755 builds/macos-arm64/dcode /opt/homebrew/bin/dcode
dcode --version
```

It passed all 31 checks of `docs/dev/terminal_e2e.py --binary`.

## Linux (x86_64), 0.8.0

SHA-256:

```
65c8f5d39b14b3f43b6ba911fe1ccd5baf9e1eb190a0673b89daa16fb12743fc  dcode-0.8.0-1.x86_64.rpm
d85c61f85dd2198f9245a78d323631d004ec7002e6b8db752d76f4b1112fb125  dcode_0.8.0_amd64.deb
402dae5f5b745ef9125185078a767708e9c829b7d231d685f19a0cdf7b0b2e7a  dcode-0.8.0-linux-x86_64.tar.gz
920d6e47fa3916fc30daf0e5d568d6389fb61a90dd39c447ee9e0435a843e7bb  dcode-0.8.0.vsix
```

```bash
sudo packaging/install-airgap.sh builds/linux-amd64/dcode_0.8.0_amd64.deb       # Debian / Ubuntu
sudo packaging/install-airgap.sh builds/linux-amd64/dcode-0.8.0-1.x86_64.rpm    # RHEL / Rocky / Alma / Fedora / SUSE
tar -xzf builds/linux-amd64/dcode-0.8.0-linux-x86_64.tar.gz && ./dcode-0.8.0-linux-x86_64/install.sh   # any Linux, no root
code --install-extension builds/linux-amd64/dcode-0.8.0.vsix                    # the VS Code extension (any OS)
dcode --version
```

- All three packages hold the same binary. Python and every dependency are inside, including the PostgreSQL and MySQL drivers.
- It was built on glibc 2.28, so it runs on RHEL 8+, Debian 10+ and Ubuntu 20.04+. The 0.8.0 `.rpm` and tarball were run in an AlmaLinux 8 container and the `.deb` in a Debian 12 container (0.7.1 also in Debian 11, 0.5.0 also in AlmaLinux 9 and Ubuntu 20.04).
- The binary passed all the checks of `docs/dev/terminal_e2e.py --binary`.
- The Docker image is not stored here (85 MB): `make docker` builds `dist/dcode-0.8.0-docker-x86_64.tar.gz`.
- The 0.4.0 `.deb` was removed: it needed glibc 2.35 and Debian 11 could not open it.
- The macOS binary above is still 0.3.1; rebuild it on a Mac. There is no arm64 Linux build.

To rebuild: `make mac PYTHON=.venv/bin/python`, then copy `dist/dcode` here.
