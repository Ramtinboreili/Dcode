#!/usr/bin/env bash
#
# Build a self-contained code-one binary with PyInstaller.
#
# Run this on a build machine whose OS + CPU architecture MATCH the target
# air-gapped server (e.g. build on Ubuntu x86_64 to deploy on Ubuntu x86_64).
# PyInstaller does not cross-compile.
#
# Output: dist/code-one  (a single executable with Python + all deps bundled)
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

PYTHON="${PYTHON:-python3}"
# On the build machine (which HAS internet) install the project + PyInstaller.
# For a fully offline build, pre-populate a wheelhouse and set:
#   PIP_ARGS="--no-index --find-links /path/to/wheelhouse"
PIP_ARGS="${PIP_ARGS:-}"

echo ">> Installing build dependencies"
"${PYTHON}" -m pip install ${PIP_ARGS} .
"${PYTHON}" -m pip install ${PIP_ARGS} pyinstaller

echo ">> Cleaning previous build"
rm -rf build dist

echo ">> Building single-file binary"
"${PYTHON}" -m PyInstaller \
  --onefile \
  --name code-one \
  --clean \
  --noconfirm \
  --collect-all prompt_toolkit \
  --collect-all rich \
  --collect-all openai \
  code_one_bootstrap.py

echo ">> Done: $(ls -lh dist/code-one | awk '{print $5, $9}')"
echo "   Test it:  ./dist/code-one --help"
