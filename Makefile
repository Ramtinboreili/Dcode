.PHONY: help install dev test lint binary mac deb clean

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
	  --add-data "dcode/web:dcode/web" --add-data "dcode/skills:dcode/skills" dcode_bootstrap.py
	install -m 0755 dist/dcode $(MAC_BIN)/dcode
	@echo "Installed $(MAC_BIN)/dcode"

deb: binary
	bash packaging/build-deb.sh

clean:
	rm -rf build dist *.egg-info dcode.egg-info
