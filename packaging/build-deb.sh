#!/usr/bin/env bash
#
# Package the self-contained code-one binary into a .deb.
#
# Prerequisites:
#   1. Run packaging/build-binary.sh first (produces dist/code-one).
#   2. dpkg-deb must be available (present on any Debian/Ubuntu machine).
#
# Output: dist/code-one_<version>_<arch>.deb
#
# The resulting .deb has NO dependencies (Python is bundled inside the binary),
# so it installs cleanly on an air-gapped Debian/Ubuntu server with `dpkg -i`.
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

BINARY="dist/code-one"
if [[ ! -f "${BINARY}" ]]; then
  echo "ERROR: ${BINARY} not found. Run packaging/build-binary.sh first." >&2
  exit 1
fi

# Read version straight from the package so it never drifts.
VERSION="$(python3 -c 'import code_one; print(code_one.__version__)')"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
PKG_NAME="code-one"
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

echo ">> Staging ${PKG_NAME} ${VERSION} (${ARCH})"
install -d "${STAGE}/DEBIAN"
install -d "${STAGE}/usr/bin"
install -d "${STAGE}/usr/share/doc/${PKG_NAME}"

install -m 0755 "${BINARY}" "${STAGE}/usr/bin/code-one"
[[ -f README.md ]] && install -m 0644 README.md "${STAGE}/usr/share/doc/${PKG_NAME}/README.md"

cat > "${STAGE}/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${ARCH}
Maintainer: code-one <noreply@example.com>
Description: Terminal agentic coding assistant for local / air-gapped LLMs
 code-one is a terminal-based AI coding assistant that connects to any
 OpenAI-compatible endpoint (Ollama, vLLM, llama.cpp, LM Studio, TGI).
 Python and all dependencies are bundled, so it runs with no network
 and no system Python required.
EOF

cat > "${STAGE}/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
echo "code-one installed. Configure it with:"
echo "  export CODE_ONE_BASE_URL=http://<llm-host>:11434/v1"
echo "  export CODE_ONE_API_KEY=local"
echo "  export CODE_ONE_MODEL=<model-name>"
echo "Then run: code-one"
exit 0
EOF
chmod 0755 "${STAGE}/DEBIAN/postinst"

OUT="dist/${PKG_NAME}_${VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group --build "${STAGE}" "${OUT}"
echo ">> Built ${OUT}"
dpkg-deb --info "${OUT}" | sed 's/^/   /'
