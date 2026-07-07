.PHONY: help install dev test lint binary deb clean

PYTHON ?= python3
# Optional: point at a private/offline PyPI mirror, e.g.
#   make binary PIP_INDEX_URL=https://your-mirror/simple
PIP_INDEX_URL ?=
PIP_INDEX_ARG := $(if $(PIP_INDEX_URL),-i $(PIP_INDEX_URL),)

help:
	@echo "Targets:"
	@echo "  install  Install code-one into the current environment"
	@echo "  dev      Install with dev extras (pytest, ruff, pyinstaller)"
	@echo "  test     Run the test suite"
	@echo "  lint     Run ruff"
	@echo "  binary   Build a self-contained binary (dist/code-one)"
	@echo "  deb      Build a .deb from the binary (Debian/Ubuntu build host)"
	@echo "  clean    Remove build artifacts"

install:
	$(PYTHON) -m pip install $(PIP_INDEX_ARG) .

dev:
	$(PYTHON) -m pip install $(PIP_INDEX_ARG) -e ".[dev]"

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check code_one tests

binary:
	bash packaging/build-binary.sh

deb: binary
	bash packaging/build-deb.sh

clean:
	rm -rf build dist *.egg-info code_one.egg-info
