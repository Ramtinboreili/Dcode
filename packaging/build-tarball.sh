#!/usr/bin/env bash
#
# Pack the binary for any Linux, with no package manager: a .tar.gz holding the binary, an
# installer that works with or without root, and the README.
#
# Prerequisite: dist/dcode (packaging/build-binary.sh or packaging/build-in-docker.sh).
# Output: dist/dcode-<version>-linux-<arch>.tar.gz
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

BINARY="dist/dcode"
[[ -f "${BINARY}" ]] || { echo "ERROR: ${BINARY} not found. Build it first." >&2; exit 1; }
PYTHON="${PYTHON:-python3}"
VERSION="$("${PYTHON}" -c 'import dcode; print(dcode.__version__)')"
ARCH="$(uname -m)"
NAME="dcode-${VERSION}-linux-${ARCH}"
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT
mkdir -p "${STAGE}/${NAME}"

install -m 0755 "${BINARY}" "${STAGE}/${NAME}/dcode"
install -m 0644 README.md "${STAGE}/${NAME}/README.md"
cat > "${STAGE}/${NAME}/install.sh" <<'INSTALL'
#!/bin/sh
# Install dcode from this folder. No network. Root installs for everyone; anyone else for themselves.
#   ./install.sh            /usr/local/bin as root, ~/.local/bin otherwise
#   ./install.sh /opt/bin   a folder of your choice
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -n "${1:-}" ]; then DEST="$1"; elif [ "$(id -u)" -eq 0 ]; then DEST=/usr/local/bin; else DEST="${HOME}/.local/bin"; fi
mkdir -p "${DEST}"
install -m 0755 "${HERE}/dcode" "${DEST}/dcode"
echo "Installed ${DEST}/dcode ($("${DEST}/dcode" --version))"
case ":${PATH}:" in *":${DEST}:"*) ;; *) echo "Add it to your PATH:  export PATH=\"${DEST}:\$PATH\"" ;; esac
echo "Run: dcode"
INSTALL
chmod 0755 "${STAGE}/${NAME}/install.sh"

tar -C "${STAGE}" --owner=0 --group=0 -czf "dist/${NAME}.tar.gz" "${NAME}"
echo ">> Built dist/${NAME}.tar.gz ($(du -h "dist/${NAME}.tar.gz" | cut -f1))"
