# Builds

Ready-made binaries, so a new machine can run Dcode without building it first.

| File | Platform | Built from | Reports version |
|---|---|---|---|
| `macos-arm64/dcode` | macOS, Apple Silicon | commit `0456097` on 2026-10-07 (0.3.1 plus the unreleased Phase 3 work) | 0.3.1 |
| `linux-amd64/` (`.deb`, `.rpm`, `.tar.gz`, `.vsix`) | Linux x86_64, any distribution with glibc 2.28 or newer (built on AlmaLinux 8 with `make packages`) | the `Release 0.5.0` commit, 2026-10-08 | 0.5.0 |

SHA-256 of `macos-arm64/dcode`: `fb577199db5ab6e731de7f2fc893fb6f2dc10930c6726ac6d06ba239b835c4d8`

Install on an Apple Silicon Mac (no sudo needed):

```bash
install -m 0755 builds/macos-arm64/dcode /opt/homebrew/bin/dcode
dcode --version
```

It passed all 31 checks of `docs/dev/terminal_e2e.py --binary`.

## Linux (x86_64), 0.5.0

SHA-256:

```
cf40d9c4bca1a5cd0970d720c73c1f914115b24b0c72521a63dbe13b9e6654a3  dcode-0.5.0-1.x86_64.rpm
b9e307daf491b9e32ce7f990e7294f9ea8dc3256adc35407ae29f209b7545698  dcode-0.5.0-linux-x86_64.tar.gz
5581855a1ac2464b79edc8100b71461e40aa8d87d8fc1d845e7a4c3abcde15ad  dcode-0.5.0.vsix
f1780249e964d2dfa99b480ac820c31840b3301ecfce4ec2d50419ca65f90de6  dcode_0.5.0_amd64.deb
```

```bash
sudo packaging/install-airgap.sh builds/linux-amd64/dcode_0.5.0_amd64.deb       # Debian / Ubuntu
sudo packaging/install-airgap.sh builds/linux-amd64/dcode-0.5.0-1.x86_64.rpm    # RHEL / Rocky / Alma / Fedora / SUSE
tar -xzf builds/linux-amd64/dcode-0.5.0-linux-x86_64.tar.gz && ./dcode-0.5.0-linux-x86_64/install.sh   # any Linux, no root
code --install-extension builds/linux-amd64/dcode-0.5.0.vsix                    # the VS Code extension (any OS)
dcode --version
```

- All three packages hold the same binary. Python and every dependency are inside, including the PostgreSQL and MySQL drivers.
- It was built on glibc 2.28, so it runs on RHEL 8+, Debian 10+ and Ubuntu 20.04+. Installed and run in AlmaLinux 8 and 9, Debian 11 and Ubuntu 20.04 containers.
- The binary passed all 59 checks of `docs/dev/terminal_e2e.py --binary`.
- The Docker image is not stored here (85 MB): `make docker` builds `dist/dcode-0.5.0-docker-x86_64.tar.gz`.
- The 0.4.0 `.deb` was removed: it needed glibc 2.35 and Debian 11 could not open it.
- The macOS binary above is still 0.3.1; rebuild it on a Mac. There is no arm64 Linux build.

To rebuild: `make mac PYTHON=.venv/bin/python`, then copy `dist/dcode` here.
