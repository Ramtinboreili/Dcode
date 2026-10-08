#!/usr/bin/env bash
#
# Build the Docker image and save it as a file for machines without a registry.
#
# Prerequisite: dist/dcode (packaging/build-in-docker.sh gives one that runs on any base image).
# Output: image dcode:<version> and dist/dcode-<version>-docker-<arch>.tar.gz  (docker load -i …)
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"
[[ -f dist/dcode ]] || { echo "ERROR: dist/dcode not found. Build it first." >&2; exit 1; }
PYTHON="${PYTHON:-python3}"
VERSION="$("${PYTHON}" -c 'import dcode; print(dcode.__version__)')"
ARCH="$(uname -m)"

CONTEXT="$(mktemp -d)"
trap 'rm -rf "${CONTEXT}"' EXIT
mkdir -p "${CONTEXT}/dist"
cp dist/dcode "${CONTEXT}/dist/dcode"   # a small context: not the whole repository
docker build -q -t "dcode:${VERSION}" -f packaging/Dockerfile "${CONTEXT}" >/dev/null
docker save "dcode:${VERSION}" | gzip > "dist/dcode-${VERSION}-docker-${ARCH}.tar.gz"
echo ">> Built image dcode:${VERSION} and dist/dcode-${VERSION}-docker-${ARCH}.tar.gz ($(du -h "dist/dcode-${VERSION}-docker-${ARCH}.tar.gz" | cut -f1))"
