# Builds

Ready-made binaries, so a new machine can run Dcode without building it first.

| File | Platform | Built from | Reports version |
|---|---|---|---|
| `macos-arm64/dcode` | macOS, Apple Silicon | commit `0456097` on 2026-10-07 (0.3.1 plus the unreleased Phase 3 work) | 0.3.1 |
| `linux-amd64/` (`.deb`, `.rpm`, `.tar.gz`, `.vsix`) | Linux x86_64, any distribution with glibc 2.28 or newer (built on AlmaLinux 8 with `make packages`) | the `Release 0.6.0` commit, 2026-10-13 | 0.6.0 |

SHA-256 of `macos-arm64/dcode`: `fb577199db5ab6e731de7f2fc893fb6f2dc10930c6726ac6d06ba239b835c4d8`

Install on an Apple Silicon Mac (no sudo needed):

```bash
install -m 0755 builds/macos-arm64/dcode /opt/homebrew/bin/dcode
dcode --version
```

It passed all 31 checks of `docs/dev/terminal_e2e.py --binary`.

## Linux (x86_64), 0.6.0

SHA-256:

```
c28f14a00f0747559a56a6edf935efe588343d21793274ce09521da582bbd8f8  dcode-0.6.0-1.x86_64.rpm
7bcc5678522cac3d53178488724b83633a73d5fb1f3f47bd541dc9396e6522d5  dcode_0.6.0_amd64.deb
9d3764d9e4e93c6f130584cb7fb7e3c9a92066160613376d15241f0c28ca4ddb  dcode-0.6.0-linux-x86_64.tar.gz
af07ec7792d0f5065de813ab0c11fb04b3d46e3041c2339b8b37f15e0e44c2fd  dcode-0.6.0.vsix
```

```bash
sudo packaging/install-airgap.sh builds/linux-amd64/dcode_0.6.0_amd64.deb       # Debian / Ubuntu
sudo packaging/install-airgap.sh builds/linux-amd64/dcode-0.6.0-1.x86_64.rpm    # RHEL / Rocky / Alma / Fedora / SUSE
tar -xzf builds/linux-amd64/dcode-0.6.0-linux-x86_64.tar.gz && ./dcode-0.6.0-linux-x86_64/install.sh   # any Linux, no root
code --install-extension builds/linux-amd64/dcode-0.6.0.vsix                    # the VS Code extension (any OS)
dcode --version
```

- All three packages hold the same binary. Python and every dependency are inside, including the PostgreSQL and MySQL drivers.
- It was built on glibc 2.28, so it runs on RHEL 8+, Debian 10+ and Ubuntu 20.04+. The 0.6.0 packages were installed and run in AlmaLinux 8 and Debian 11 containers (0.5.0 also in AlmaLinux 9 and Ubuntu 20.04).
- The binary passed all 64 checks of `docs/dev/terminal_e2e.py --binary`.
- The Docker image is not stored here (85 MB): `make docker` builds `dist/dcode-0.6.0-docker-x86_64.tar.gz`.
- The 0.4.0 `.deb` was removed: it needed glibc 2.35 and Debian 11 could not open it.
- The macOS binary above is still 0.3.1; rebuild it on a Mac. There is no arm64 Linux build.

To rebuild: `make mac PYTHON=.venv/bin/python`, then copy `dist/dcode` here.
