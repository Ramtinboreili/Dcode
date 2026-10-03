#!/usr/bin/env bash
#
# Offline installer for an air-gapped server. No internet required.
#
# Copy this script together with EITHER:
#   * a dcode_*.deb  (Debian/Ubuntu), or
#   * a raw `dcode` binary
# onto the target machine, then run it.
#
# Usage:
#   ./install-airgap.sh                 # auto-detect artifact in current dir
#   ./install-airgap.sh dcode.deb    # install a specific .deb
#   ./install-airgap.sh ./dcode      # install a specific raw binary
#
set -euo pipefail

TARGET_DIR="/usr/local/bin"
BINARY_NAME="dcode"

as_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "ERROR: need root (or sudo) to install to system paths." >&2
    exit 1
  fi
}

ARTIFACT="${1:-}"
if [[ -z "${ARTIFACT}" ]]; then
  # Auto-detect: prefer a .deb, fall back to a raw binary next to this script.
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  ARTIFACT="$(ls "${HERE}"/dcode_*.deb 2>/dev/null | head -n1 || true)"
  [[ -z "${ARTIFACT}" && -f "${HERE}/dcode" ]] && ARTIFACT="${HERE}/dcode"
fi

if [[ -z "${ARTIFACT}" || ! -e "${ARTIFACT}" ]]; then
  echo "ERROR: no artifact found. Pass a .deb or binary path." >&2
  exit 1
fi

case "${ARTIFACT}" in
  *.deb)
    if ! command -v dpkg >/dev/null 2>&1; then
      echo "ERROR: dpkg not available; use the raw binary instead." >&2
      exit 1
    fi
    echo ">> Installing ${ARTIFACT} with dpkg"
    as_root dpkg -i "${ARTIFACT}"
    ;;
  *)
    echo ">> Installing raw binary to ${TARGET_DIR}/${BINARY_NAME}"
    as_root install -m 0755 "${ARTIFACT}" "${TARGET_DIR}/${BINARY_NAME}"
    ;;
esac

echo ""
echo "✓ dcode installed."
echo ""
echo "Point it at your local LLM (no data leaves the network):"
echo "  export DCODE_BASE_URL=http://<llm-host>:11434/v1   # Ollama example"
echo "  export DCODE_API_KEY=local"
echo "  export DCODE_MODEL=qwen2.5-coder:7b"
echo ""
echo "Then run:  dcode"
