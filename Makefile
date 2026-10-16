.PHONY: help install dev test lint binary mac deb linux rpm tarball docker vsix packages clean

PYTHON ?= python3
# Optional: point at a private/offline PyPI mirror, e.g.
#   make binary PIP_INDEX_URL=https://your-mirror/simple
PIP_INDEX_URL ?=
PIP_INDEX_ARG := $(if $(PIP_INDEX_URL),-i $(PIP_INDEX_URL),)

help:
	@echo "Targets:"
	@echo "  install  Install dcode into the current environment"
	@echo "  dev      Install with dev extras (pytest, ruff, pyinstaller)"
	@echo "  test     Run the test suite"
	@echo "  lint     Run ruff"
	@echo "  binary   Build a self-contained binary (dist/dcode)"
	@echo "  mac      Build dist/dcode with the current venv and install it to /opt/homebrew/bin"
	@echo "  deb      Build a .deb from the binary (Debian/Ubuntu build host)"
	@echo "  linux    Build dist/dcode and the .rpm in an old-glibc container (runs on RHEL 8+, Debian 10+, Ubuntu 20.04+)"
	@echo "  packages linux + .deb + .tar.gz + Docker image + .vsix, all from that one binary"
	@echo "  rpm | tarball | docker | vsix   One of them, from an existing dist/dcode"
	@echo "  clean    Remove build artifacts"

install:
	$(PYTHON) -m pip install $(PIP_INDEX_ARG) .

dev:
	$(PYTHON) -m pip install $(PIP_INDEX_ARG) -e ".[dev]"

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check dcode tests

binary:
	bash packaging/build-binary.sh

# Local macOS build: reuses the dev environment (no pip install), then installs
# where Homebrew's bin already sits on PATH (no sudo needed).
MAC_BIN ?= /opt/homebrew/bin
mac:
	rm -rf build dist
	$(PYTHON) -m PyInstaller --onefile --name dcode --clean --noconfirm \
	  --collect-all prompt_toolkit --collect-all rich --collect-all openai \
	  --hidden-import pg8000 --hidden-import pymysql \
	  --add-data "dcode/web:dcode/web" --add-data "dcode/skills:dcode/skills" --add-data "dcode/manager/web:dcode/manager/web" dcode_bootstrap.py
	install -m 0755 dist/dcode $(MAC_BIN)/dcode
	@echo "Installed $(MAC_BIN)/dcode"

deb: binary
	bash packaging/build-deb.sh

# One portable binary (built on glibc 2.28), then every package from it.
linux:
	bash packaging/build-in-docker.sh

rpm:
	bash packaging/build-rpm.sh

tarball:
	PYTHON=$(PYTHON) bash packaging/build-tarball.sh

docker:
	PYTHON=$(PYTHON) bash packaging/build-docker.sh

vsix:
	$(PYTHON) editors/vscode/build_vsix.py dist

packages: linux tarball docker vsix
	bash packaging/build-deb.sh
	@ls -lh dist | awk 'NR>1 {print "  " $$5, $$9}'

clean:
	rm -rf build dist *.egg-info dcode.egg-info
