#!/usr/bin/env bash
#
# Package the self-contained dcode binary into an .rpm (RHEL, Rocky, Alma, Fedora, SUSE).
#
# Prerequisites: dist/dcode (packaging/build-binary.sh) and rpmbuild (the rpm-build package).
# packaging/build-in-docker.sh does both in a container.
#
# Output: dist/dcode-<version>-1.<arch>.rpm, with no dependencies: Python is inside the binary.
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

BINARY="dist/dcode"
[[ -f "${BINARY}" ]] || { echo "ERROR: ${BINARY} not found. Run packaging/build-binary.sh first." >&2; exit 1; }
command -v rpmbuild >/dev/null 2>&1 || { echo "ERROR: rpmbuild not found (install rpm-build), or use packaging/build-in-docker.sh." >&2; exit 1; }

PYTHON="${PYTHON:-python3}"
VERSION="$("${PYTHON}" -c 'import dcode; print(dcode.__version__)')"
ARCH="$(uname -m)"
TOP="$(mktemp -d)"
trap 'rm -rf "${TOP}"' EXIT
mkdir -p "${TOP}"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
cp "${BINARY}" "${TOP}/SOURCES/dcode"
cp README.md "${TOP}/SOURCES/README.md"

cat > "${TOP}/SPECS/dcode.spec" <<SPEC
Name:           dcode
Version:        ${VERSION//-/_}
Release:        1
Summary:        Coding agent for local and air-gapped LLMs
License:        Proprietary
URL:            https://github.com/Ramtinboreili/Dcode
BuildArch:      ${ARCH}
# The binary carries its own Python and libraries: nothing to require, nothing to scan.
AutoReqProv:    no
# Stripping or repacking a PyInstaller binary cuts off the archive appended to it.
%global debug_package %{nil}
%global __os_install_post %{nil}
%global __strip /bin/true
%global _build_id_links none

%description
Dcode is a terminal coding agent with a per-user localhost web UI. It connects to any
OpenAI-compatible endpoint (Ollama, vLLM, llama.cpp, LM Studio, TGI). Python and every
dependency are bundled, so it needs no network and no system Python.

%install
install -D -m 0755 %{_sourcedir}/dcode %{buildroot}%{_bindir}/dcode
install -D -m 0644 %{_sourcedir}/README.md %{buildroot}%{_docdir}/dcode/README.md

%files
%{_bindir}/dcode
%doc %{_docdir}/dcode/README.md

%post
echo "dcode installed. Run: dcode   (first start asks for the model server's address)"
SPEC

rpmbuild --quiet -bb --define "_topdir ${TOP}" "${TOP}/SPECS/dcode.spec"
OUT="$(ls "${TOP}"/RPMS/*/*.rpm)"
cp "${OUT}" dist/
echo ">> Built dist/$(basename "${OUT}")"
