#!/usr/bin/env bash

set -euo pipefail

STORAGE_BASE_URL="${DCODE_STORAGE_URL:-https://ramtindev.storage.c2.liara.space/}"
TARGET_DIR="/usr/local/bin"
BINARY_NAME="dcode"
TMP_DIR="$(mktemp -d)"

trap 'rm -rf "${TMP_DIR}"' EXIT

case "$(uname -s)" in
  Linux) OS="linux" ;;
  Darwin) OS="macos" ;;
  *)
    echo "Unsupported OS: $(uname -s)" >&2
    exit 1
    ;;
esac

case "$(uname -m)" in
  x86_64|amd64) ARCH="x86_64" ;;
  arm64|aarch64) ARCH="arm64" ;;
  *)
    echo "Unsupported architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

if command -v curl >/dev/null 2>&1; then
  DOWNLOAD=(curl -fsSL)
else
  echo "curl is required to download the binary" >&2
  exit 1
fi

BINARY_URL="${STORAGE_BASE_URL%/}/${OS}-${ARCH}/dcode"
TMP_BINARY="${TMP_DIR}/dcode"

if ! "${DOWNLOAD[@]}" "${BINARY_URL}" -o "${TMP_BINARY}"; then
  echo "Failed to download binary: ${BINARY_URL}" >&2
  echo "Expected uploaded artifact at: ${OS}-${ARCH}/dcode" >&2
  exit 1
fi
chmod +x "${TMP_BINARY}"

if [ -w "${TARGET_DIR}" ]; then
  install -m 0755 "${TMP_BINARY}" "${TARGET_DIR}/${BINARY_NAME}"
else
  if command -v sudo >/dev/null 2>&1; then
    sudo install -m 0755 "${TMP_BINARY}" "${TARGET_DIR}/${BINARY_NAME}"
  else
    echo "No permission to write to ${TARGET_DIR} and sudo is unavailable" >&2
    exit 1
  fi
fi

echo "✓ Done! Run: dcode"
