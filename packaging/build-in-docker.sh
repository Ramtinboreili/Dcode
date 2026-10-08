#!/usr/bin/env bash
#
# Build the Linux binary (and the .rpm) inside an old-glibc container, so one binary runs on
# old and new distributions alike.
#
# A PyInstaller binary needs a glibc at least as new as the one it was built on. Built on
# Ubuntu 22.04 it needs glibc 2.35, which leaves out RHEL 8/9, Debian 11 and Ubuntu 20.04.
# Built here, on AlmaLinux 8 (glibc 2.28), it runs on all of them.
#
# Needs: docker, and network access to pull the image and the Python packages
# (set PIP_INDEX_URL for a private mirror). The CPU is the host's: no cross-building.
#
# Output: dist/dcode  and  dist/dcode-<version>-1.<arch>.rpm
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${DCODE_BUILD_IMAGE:-almalinux:8}"
PIP_INDEX_URL="${PIP_INDEX_URL:-}"

mkdir -p "${ROOT_DIR}/dist"
echo ">> Building in ${IMAGE} (glibc 2.28)"
docker run --rm --name dcode-build \
  -v "${ROOT_DIR}:/src:ro" -v "${ROOT_DIR}/dist:/out" \
  -e PIP_INDEX_URL="${PIP_INDEX_URL}" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  "${IMAGE}" bash -euo pipefail -c '
    dnf install -y -q python3.11 python3.11-pip binutils rpm-build >/dev/null
    # Work on a copy: the build writes build/, dist/ and an egg-info next to the sources.
    mkdir /work && cd /src && tar --exclude=./dist --exclude=./build --exclude=./.venv --exclude=./.git --exclude=./builds -cf - . | tar -xf - -C /work
    cd /work
    python3.11 -m venv /venv
    /venv/bin/pip install -q --upgrade pip
    PYTHON=/venv/bin/python bash packaging/build-binary.sh
    ./dist/dcode --version
    PYTHON=/venv/bin/python bash packaging/build-rpm.sh
    cp dist/dcode dist/*.rpm /out/
    chown "${HOST_UID}:${HOST_GID}" /out/dcode /out/*.rpm
  '
echo ">> Done:"
ls -lh "${ROOT_DIR}/dist/dcode" "${ROOT_DIR}"/dist/*.rpm | awk '{print "   " $5, $9}'
